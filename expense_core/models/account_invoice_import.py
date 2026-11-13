import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class AccountInvoiceImport(models.TransientModel):
    _inherit = 'account.invoice.import'

    @api.model
    def fallback_parse_pdf_invoice(self, file_data, company):
        """Dernier recours de l'import d'une facture en PDF, après les XML Factur-X et UBL : l'OCR lit le PDF avec le
        modèle de factures. Un seul traitement pour tous les fournisseurs, sans modèle propre à chacun.

        Sans service OCR, ou si la lecture échoue, la facture n'est pas importée comme avant (aucun changement).
        """
        parsed = super().fallback_parse_pdf_invoice(file_data, company)
        if parsed:
            return parsed
        ocr = self.env['expense.ocr']
        if not ocr.is_configured():
            return parsed
        try:
            result = ocr.analyze(file_data, 'application/pdf', kind='invoice')
        except Exception as error:  # noqa: BLE001 : un échec de l'OCR ne doit pas bloquer le courrier entrant
            _logger.warning("Lecture OCR de la facture impossible : %s", error)
            return parsed
        return self._ocr_invoice_to_parsed(result)

    @api.model
    def _ocr_invoice_to_parsed(self, result):
        """Traduit le résultat de expense.ocr (kind='invoice') dans le format pivot de l'import de factures."""
        parsed = {
            'type': 'in_invoice',
            'chatter_msg': [self.env._("Facture lue par OCR : à vérifier par l'Office avant le bon à payer.")],
        }
        if result.get('currency'):
            parsed['currency'] = {'iso': result['currency']}
        for key, value in (('date', result.get('date')), ('date_due', result.get('due_date'))):
            if value:
                parsed[key] = value.isoformat()
        if result.get('invoice_number'):
            parsed['invoice_number'] = result['invoice_number']
        if result.get('total') is not None:
            parsed['amount_total'] = result['total']
        if result.get('untaxed') is not None:
            parsed['amount_untaxed'] = result['untaxed']
        elif result.get('tax_total') is not None:
            parsed['amount_tax'] = result['tax_total']
        address = result.get('address') or {}
        partner = {
            'name': result.get('vendor'), 'vat': result.get('vendor_vat'),
            'street': address.get('street'), 'city': address.get('city'), 'zip': address.get('zip'),
            'country_code': address.get('country_code'),
        }
        parsed['partner'] = {key: value for key, value in partner.items() if value}
        if result.get('iban'):
            parsed['iban'] = result['iban']
            if result.get('bic'):
                parsed['bic'] = result['bic']
        # Le taux d'une ligne vient de la ligne ; à défaut, le seul taux du récapitulatif de TVA.
        rates = {d['rate'] for d in result.get('tax_details') or [] if d.get('rate') is not None}
        default_rate = next(iter(rates)) if len(rates) == 1 else None
        lines = []
        for line in result.get('lines') or []:
            rate = line.get('tax_rate') if line.get('tax_rate') is not None else default_rate
            qty = line.get('qty') or 1.0
            price_unit = line.get('price_unit')
            if price_unit is None and line.get('amount') is not None:
                price_unit = line['amount'] / qty
            entry = {'name': line.get('name') or self.env._("Ligne lue par OCR"), 'qty': qty,
                     'price_unit': price_unit or 0.0}
            if line.get('amount') is not None:
                entry['price_subtotal'] = line['amount']
            if rate is not None:
                entry['taxes'] = [{'amount_type': 'percent', 'amount': rate, 'unece_type_code': 'VAT',
                                   'unece_categ_code': 'S'}]
            lines.append(entry)
        if not lines and result.get('untaxed') is not None:
            entry = {'name': self.env._("Facture lue par OCR"), 'qty': 1.0, 'price_unit': result['untaxed']}
            if default_rate is not None:
                entry['taxes'] = [{'amount_type': 'percent', 'amount': default_rate, 'unece_type_code': 'VAT',
                                   'unece_categ_code': 'S'}]
            lines.append(entry)
        if lines:
            parsed['lines'] = lines
        return parsed
