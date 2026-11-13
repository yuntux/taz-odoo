import logging
import re
import time
from datetime import date

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

AZURE_API_VERSION = '2024-11-30'
AZURE_POLL_DELAY = 1.0
AZURE_POLL_ATTEMPTS = 30
AZURE_TIMEOUT = 30


class ExpenseOcr(models.AbstractModel):
    """Service de lecture des justificatifs.

    Un seul point d'entrée, analyze(), qui renvoie un résultat normalisé quel que soit le fournisseur :

        {'merchant': str, 'date': date | None, 'total': float | None, 'tax_total': float | None,
         'currency': str | None, 'lines': [{'name': str, 'amount': float TTC, 'tax_rate': float | None}],
         'tax_details': [{'rate': float | None, 'net': float | None, 'tax': float | None}],
         'confidence': float}

    tax_details est le récapitulatif de TVA par taux imprimé au pied du ticket (TaxDetails d'Azure).

    Un autre fournisseur s'ajoute par une méthode _analyze_<fournisseur> et une valeur de
    le paramètre système expense_core.ocr_provider, sans toucher aux circuits de dépenses.
    """
    _name = 'expense.ocr'
    _description = "Service de lecture des justificatifs"

    @api.model
    def _get_param(self, name):
        return self.env['ir.config_parameter'].sudo().get_param(f'expense_core.ocr_{name}') or ''

    @api.model
    def is_configured(self):
        return bool(self._get_param('provider'))

    def analyze(self, content, mimetype, kind='receipt'):
        """kind='receipt' : ticket ou reçu (modèle de reçus) ; kind='invoice' : facture de fournisseur (modèle de
        factures, résultat au format de analyze_invoice)."""
        provider = self._get_param('provider')
        if not provider:
            raise UserError(_("Aucun service OCR n'est configuré (Paramètres > Comptabilité)."))
        method = getattr(self, f'_analyze_{provider}')
        return method(content, mimetype) if kind == 'receipt' else method(content, mimetype, kind=kind)

    # ------------------------------------------------------------------
    # Azure Document Intelligence
    # ------------------------------------------------------------------
    def _analyze_azure(self, content, mimetype, kind='receipt'):
        endpoint = self._get_param('endpoint').rstrip('/')
        key = self._get_param('api_key')
        if not endpoint or not key:
            raise UserError(_("L'adresse et la clé du service OCR doivent être renseignées."))
        if kind == 'invoice':
            model = self._get_param('invoice_model') or 'prebuilt-invoice'
        else:
            model = self._get_param('model') or 'prebuilt-receipt'
        headers = {'Ocp-Apim-Subscription-Key': key}
        url = f'{endpoint}/documentintelligence/documentModels/{model}:analyze'
        response = requests.post(
            url, params={'api-version': AZURE_API_VERSION}, data=content,
            headers={**headers, 'Content-Type': 'application/octet-stream'}, timeout=AZURE_TIMEOUT)
        if response.status_code != 202:
            raise UserError(_("Le service OCR a refusé le document (code %s).", response.status_code))
        operation_url = response.headers.get('Operation-Location')
        if not operation_url:
            raise UserError(_("Le service OCR n'a pas indiqué où lire le résultat."))
        for _attempt in range(AZURE_POLL_ATTEMPTS):
            result = requests.get(operation_url, headers=headers, timeout=AZURE_TIMEOUT)
            if result.status_code != 200:
                raise UserError(_("Lecture du résultat OCR impossible (code %s).", result.status_code))
            data = result.json()
            status = data.get('status')
            if status == 'succeeded':
                return self._normalize_azure_invoice(data) if kind == 'invoice' else self._normalize_azure(data)
            if status == 'failed':
                raise UserError(_("Le service OCR n'a pas pu lire le document."))
            time.sleep(AZURE_POLL_DELAY)
        raise UserError(_("Le service OCR n'a pas répondu à temps."))

    @staticmethod
    def _parse_rate(field):
        """Taux de TVA d'un champ Azure : un nombre (receipt) ou un texte comme « 18 % » (invoice)."""
        if not field:
            return None
        if field.get('valueNumber') is not None:
            return float(field['valueNumber'])
        match = re.search(r'\d+(?:[.,]\d+)?', field.get('valueString') or field.get('content') or '')
        return float(match.group().replace(',', '.')) if match else None

    @classmethod
    def _normalize_azure(cls, data):
        documents = (data.get('analyzeResult') or {}).get('documents') or []
        fields = documents[0].get('fields', {}) if documents else {}
        if not fields:
            raise UserError(_("Aucune information n'a été lue sur le document."))

        def text(name):
            return (fields.get(name) or {}).get('valueString') or (fields.get(name) or {}).get('content')

        def amount(name):
            value = (fields.get(name) or {}).get('valueCurrency') or {}
            return value.get('amount'), value.get('currencyCode')

        total, currency = amount('Total')
        tax_total, tax_currency = amount('TotalTax')
        day = (fields.get('TransactionDate') or {}).get('valueDate')
        lines = []
        for item in (fields.get('Items') or {}).get('valueArray', []):
            props = item.get('valueObject', {})
            line_amount = (props.get('TotalPrice') or {}).get('valueCurrency', {}).get('amount')
            name = (props.get('Description') or {}).get('valueString') or (props.get('Description') or {}).get('content')
            rate = cls._parse_rate(props.get('TaxRate'))
            if name or line_amount is not None:
                lines.append({'name': name or '', 'amount': line_amount, 'tax_rate': rate})
        tax_details = []
        for detail in (fields.get('TaxDetails') or {}).get('valueArray', []):
            props = detail.get('valueObject', {})
            tax_details.append({
                'rate': cls._parse_rate(props.get('Rate')),
                'net': (props.get('NetAmount') or {}).get('valueCurrency', {}).get('amount'),
                'tax': (props.get('Amount') or {}).get('valueCurrency', {}).get('amount'),
            })
        confidences = [
            fields[name].get('confidence') for name in ('MerchantName', 'TransactionDate', 'Total')
            if name in fields and fields[name].get('confidence') is not None]
        return {
            'merchant': text('MerchantName') or '',
            'date': date.fromisoformat(day) if day else None,
            'total': total,
            'tax_total': tax_total,
            'currency': currency or tax_currency,
            'lines': lines,
            'tax_details': tax_details,
            'confidence': min(confidences) if confidences else 0.0,
        }

    @classmethod
    def _normalize_azure_invoice(cls, data):
        """Résultat du modèle de factures d'Azure (prebuilt-invoice), indépendant du fournisseur d'OCR :

            {'vendor': str, 'vendor_vat': str, 'invoice_number': str, 'date': date | None, 'due_date': date | None,
             'total': float | None, 'untaxed': float | None, 'tax_total': float | None, 'currency': str | None,
             'iban': str, 'bic': str,
             'address': {'street': str, 'city': str, 'zip': str, 'country_code': str},
             'lines': [{'name': str, 'qty': float, 'price_unit': float | None, 'amount': float | None,
                        'tax_rate': float | None}],
             'tax_details': [{'rate': float | None, 'net': float | None, 'tax': float | None}]}
        """
        documents = (data.get('analyzeResult') or {}).get('documents') or []
        fields = documents[0].get('fields', {}) if documents else {}
        if not fields:
            raise UserError(_("Aucune information n'a été lue sur le document."))

        def text(name, source=None):
            field = (source if source is not None else fields).get(name) or {}
            return (field.get('valueString') or field.get('content') or '').strip()

        def money(field):
            value = (field or {}).get('valueCurrency') or {}
            return value.get('amount'), value.get('currencyCode')

        def day(name):
            value = (fields.get(name) or {}).get('valueDate')
            return date.fromisoformat(value) if value else None

        total, currency = money(fields.get('InvoiceTotal'))
        untaxed, untaxed_currency = money(fields.get('SubTotal'))
        tax_total, _tax_currency = money(fields.get('TotalTax'))
        address = (fields.get('VendorAddress') or {}).get('valueAddress') or {}
        iban = bic = ''
        for payment in (fields.get('PaymentDetails') or {}).get('valueArray', []):
            props = payment.get('valueObject', {})
            iban = iban or text('IBAN', props).replace(' ', '')
            bic = bic or text('SWIFT', props).replace(' ', '')
        lines = []
        for item in (fields.get('Items') or {}).get('valueArray', []):
            props = item.get('valueObject', {})
            amount, _line_currency = money(props.get('Amount'))
            unit_price, _unit_currency = money(props.get('UnitPrice'))
            name = text('Description', props)
            if name or amount is not None:
                lines.append({
                    'name': name,
                    'qty': (props.get('Quantity') or {}).get('valueNumber') or 1.0,
                    'price_unit': unit_price, 'amount': amount,
                    'tax_rate': cls._parse_rate(props.get('TaxRate')),
                })
        tax_details = []
        for detail in (fields.get('TaxDetails') or {}).get('valueArray', []):
            props = detail.get('valueObject', {})
            tax_details.append({
                'rate': cls._parse_rate(props.get('Rate')),
                'net': money(props.get('NetAmount'))[0],
                'tax': money(props.get('Amount'))[0],
            })
        return {
            'vendor': text('VendorName'),
            'vendor_vat': text('VendorTaxId').replace(' ', ''),
            'invoice_number': text('InvoiceId'),
            'date': day('InvoiceDate'),
            'due_date': day('DueDate'),
            'total': total, 'untaxed': untaxed, 'tax_total': tax_total,
            'currency': currency or untaxed_currency,
            'iban': iban, 'bic': bic,
            'address': {
                'street': address.get('streetAddress') or '', 'city': address.get('city') or '',
                'zip': address.get('postalCode') or '', 'country_code': address.get('countryRegion') or ''},
            'lines': lines,
            'tax_details': tax_details,
        }
