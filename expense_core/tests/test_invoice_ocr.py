import base64
from datetime import date
from io import BytesIO
from unittest.mock import Mock, patch

from pypdf import PdfWriter

from odoo import Command
from odoo.tests.common import TransactionCase, tagged

from ..models import expense_ocr

REQUESTS = 'odoo.addons.expense_core.models.expense_ocr.requests'


def money(amount):
    return {'valueCurrency': {'amount': amount, 'currencyCode': 'EUR'}}


def invoice_response(items=None, details=None):
    items = items if items is not None else [('Prestation de conseil', 2, 50.0, 100.0, '20%')]
    array = []
    for name, qty, unit, amount, rate in items:
        props = {'Description': {'valueString': name}, 'Quantity': {'valueNumber': qty},
                 'UnitPrice': money(unit), 'Amount': money(amount)}
        if rate:
            props['TaxRate'] = {'valueString': rate}
        array.append({'valueObject': props})
    fields = {
        'VendorName': {'valueString': 'ACME CONSEIL SAS'},
        'VendorTaxId': {'valueString': 'FR 12 345678901'},
        'VendorAddress': {'valueAddress': {'streetAddress': '1 rue des Lilas', 'city': 'Lyon',
                                           'postalCode': '69003', 'countryRegion': 'FR'}},
        'InvoiceId': {'valueString': 'F-2026-001'},
        'InvoiceDate': {'valueDate': '2026-11-05'},
        'DueDate': {'valueDate': '2026-12-05'},
        'SubTotal': money(100.0), 'TotalTax': money(20.0), 'InvoiceTotal': money(120.0),
        'PaymentDetails': {'valueArray': [{'valueObject': {
            'IBAN': {'valueString': 'FR14 2004 1010 0505 0001 3M02 606'}, 'SWIFT': {'valueString': 'PSSTFRPPXXX'}}}]},
        'Items': {'valueArray': array},
    }
    if details:
        fields['TaxDetails'] = {'valueArray': [{'valueObject': {
            'Rate': {'valueString': rate}, 'Amount': money(amount)}} for rate, amount in details]}
    return {'status': 'succeeded', 'analyzeResult': {'documents': [{'fields': fields}]}}


def blank_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@tagged('post_install', '-at_install')
class TestInvoiceOcr(TransactionCase):
    """UC8 et UC9 : une facture PDF sans XML est lue par l'OCR (modèle de factures), pour tous les fournisseurs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        params = cls.env['ir.config_parameter'].sudo()
        params.set_param('expense_core.ocr_provider', 'azure')
        params.set_param('expense_core.ocr_endpoint', 'https://ocr.example.com/')
        params.set_param('expense_core.ocr_api_key', 'cle-de-test')
        params.set_param('expense_core.ocr_invoice_model', '')
        cls.env['account.tax'].search([('type_tax_use', '=', 'purchase'), ('amount', '=', 20),
                                       ('company_id', '=', cls.company.id)]) or cls.env['account.tax'].create({
            'name': 'TVA 20 % test OCR', 'amount': 20, 'type_tax_use': 'purchase', 'company_id': cls.company.id})

    def _azure(self, data=None):
        post = Mock(status_code=202, headers={'Operation-Location': 'https://ocr.example.com/op/1'})
        get = Mock(status_code=200)
        get.json.return_value = data or invoice_response()
        self.post_mock = Mock(return_value=post)
        return patch.multiple(REQUESTS, post=self.post_mock, get=Mock(return_value=get))

    def _parse(self, data=None):
        wizard = self.env['account.invoice.import']
        with self._azure(data):
            return wizard.parse_invoice(base64.b64encode(blank_pdf()), 'facture.pdf', self.company)

    def test_the_invoice_model_is_used_and_the_result_normalized(self):
        with self._azure(), patch.object(expense_ocr.time, 'sleep'):
            result = self.env['expense.ocr'].analyze(b'x', 'application/pdf', kind='invoice')
        self.assertIn('prebuilt-invoice:analyze', self.post_mock.call_args[0][0])
        self.assertEqual(result['vendor'], 'ACME CONSEIL SAS')
        self.assertEqual(result['vendor_vat'], 'FR12345678901')
        self.assertEqual(result['iban'], 'FR1420041010050500013M02606')
        self.assertEqual(result['bic'], 'PSSTFRPPXXX')
        self.assertEqual(result['date'], date(2026, 11, 5))
        self.assertEqual(result['lines'][0]['tax_rate'], 20.0)
        self.assertEqual(result['lines'][0]['price_unit'], 50.0)

    def test_the_invoice_model_is_a_parameter(self):
        self.env['ir.config_parameter'].sudo().set_param('expense_core.ocr_invoice_model', 'prebuilt-custom')
        with self._azure(), patch.object(expense_ocr.time, 'sleep'):
            self.env['expense.ocr'].analyze(b'x', 'application/pdf', kind='invoice')
        self.assertIn('prebuilt-custom:analyze', self.post_mock.call_args[0][0])

    def test_a_receipt_still_uses_the_receipt_model(self):
        with self._azure(), patch.object(expense_ocr.time, 'sleep'):
            try:
                self.env['expense.ocr'].analyze(b'x', 'image/png')
            except Exception:  # noqa: BLE001 : seul le modèle appelé compte ici
                pass
        self.assertIn('prebuilt-receipt:analyze', self.post_mock.call_args[0][0])

    def test_a_pdf_without_xml_is_read_by_the_ocr(self):
        with patch.object(expense_ocr.time, 'sleep'):
            parsed = self._parse()
        self.assertEqual(parsed['type'], 'in_invoice')
        self.assertEqual(parsed['invoice_number'], 'F-2026-001')
        self.assertEqual(parsed['amount_total'], 120.0)
        self.assertEqual(parsed['partner']['vat'], 'FR12345678901')
        self.assertEqual(parsed['iban'], 'FR1420041010050500013M02606')
        line = parsed['lines'][0]
        self.assertEqual((line['qty'], line['price_unit']), (2, 50.0))
        self.assertEqual(line['taxes'][0]['amount'], 20.0)

    def test_the_single_rate_of_the_vat_recap_applies_to_lines_without_rate(self):
        data = invoice_response(items=[('Prestation', 1, 100.0, 100.0, None)], details=[('20%', 20.0)])
        with patch.object(expense_ocr.time, 'sleep'):
            parsed = self._parse(data)
        self.assertEqual(parsed['lines'][0]['taxes'][0]['amount'], 20.0)

    def test_a_pdf_without_lines_gives_one_line(self):
        data = invoice_response(items=[], details=[('20%', 20.0)])
        with patch.object(expense_ocr.time, 'sleep'):
            parsed = self._parse(data)
        self.assertEqual(len(parsed['lines']), 1)
        self.assertEqual(parsed['lines'][0]['price_unit'], 100.0)

    def test_the_draft_invoice_is_created_with_the_ocr_data(self):
        with patch.object(expense_ocr.time, 'sleep'):
            parsed = self._parse()
        config = {'company': self.company, 'single_line': False}
        invoice = self.env['account.invoice.import'].create_invoice(parsed, config)
        self.assertEqual(invoice.state, 'draft')
        self.assertEqual(invoice.move_type, 'in_invoice')
        self.assertEqual(invoice.ref, 'F-2026-001')
        self.assertEqual(invoice.invoice_date, date(2026, 11, 5))
        self.assertAlmostEqual(invoice.amount_total, 120.0)
        # Fournisseur inconnu : la facture reste sans tiers pour l'Office, qui est averti dans le chatter.
        self.assertFalse(invoice.partner_id)
        self.assertTrue(any('lue par OCR' in (m.body or '') for m in invoice.message_ids))

    def test_nothing_changes_without_a_configured_service(self):
        self.env['ir.config_parameter'].sudo().set_param('expense_core.ocr_provider', '')
        wizard = self.env['account.invoice.import']
        self.assertFalse(wizard.fallback_parse_pdf_invoice(blank_pdf(), self.company))

    def test_a_failing_service_leaves_the_import_as_before(self):
        post = Mock(status_code=500, headers={})
        with patch.multiple(REQUESTS, post=Mock(return_value=post)):
            self.assertFalse(self.env['account.invoice.import'].fallback_parse_pdf_invoice(
                blank_pdf(), self.company))
