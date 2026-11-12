from datetime import date
from unittest.mock import Mock, patch

from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from ..models import expense_ocr

REQUESTS = 'odoo.addons.expense_core.models.expense_ocr.requests'


def money(amount):
    return {'valueCurrency': {'amount': amount, 'currencyCode': 'EUR'}}


def azure_response(total=38.4, tax=3.49, confidence=0.97, merchant='TAXI PARISIEN', items=None, rate=None,
                   details=None):
    if items is None:
        items = [('Course', total)]
    array = []
    for name, amount in items:
        props = {'Description': {'valueString': name}, 'TotalPrice': money(amount)}
        if rate is not None:
            props['TaxRate'] = {'valueNumber': rate}
        array.append({'valueObject': props})
    fields = {
        'MerchantName': {'valueString': merchant, 'confidence': 0.99},
        'TransactionDate': {'valueDate': '2026-11-10', 'confidence': 0.98},
        'Total': dict(money(total), confidence=confidence),
        'Items': {'valueArray': array},
    }
    if tax is not None:
        fields['TotalTax'] = money(tax)
    if details:
        fields['TaxDetails'] = {'valueArray': [{'valueObject': {
            'Rate': {'valueNumber': rate}, 'NetAmount': money(net), 'Amount': money(amount)}}
            for rate, net, amount in details]}
    return {'status': 'succeeded', 'analyzeResult': {'documents': [{'fields': fields}]}}


@tagged('post_install', '-at_install')
class TestExpenseOcr(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        params = cls.env['ir.config_parameter'].sudo()
        params.set_param('expense_core.ocr_provider', 'azure')
        params.set_param('expense_core.ocr_endpoint', 'https://ocr.example.com/')
        params.set_param('expense_core.ocr_api_key', 'cle-de-test')
        cls.partner = cls.env['res.partner'].create({'name': 'Salarié OCR'})
        cls.counter = 0
        Tax = cls.env['account.tax']
        cls.category = cls.env['product.product'].create({
            'name': 'Catégorie de dépense test', 'type': 'service', 'supplier_taxes_id': False})
        cls.tax10 = Tax.create({'name': 'TVA 10% TTC test', 'amount': 10, 'price_include_override': 'tax_included',
                                'type_tax_use': 'purchase', 'company_id': cls.company.id})
        cls.tax20 = Tax.create({'name': 'TVA 20% TTC test', 'amount': 20, 'price_include_override': 'tax_included',
                                'type_tax_use': 'purchase', 'company_id': cls.company.id})

    def _regime_non_deductible(self):
        """Position fiscale de frais « non déductible » et une catégorie qui l'utilise (créées pour le test)."""
        Tax = self.env['account.tax']
        values = {'sequence': 1, 'price_include_override': 'tax_included', 'type_tax_use': 'purchase',
                  'company_id': self.company.id}
        self.tax10_nd = Tax.create(dict(values, name='TVA 10% TTC non déductible test', amount=10))
        self.tax20_nd = Tax.create(dict(values, name='TVA 20% TTC non déductible test', amount=20))
        self.position_nd = self.env['account.fiscal.position'].create({
            'name': 'Frais non déductibles test', 'company_id': self.company.id,
            'tax_ids': [Command.create({'tax_src_id': self.tax10.id, 'tax_dest_id': self.tax10_nd.id}),
                        Command.create({'tax_src_id': self.tax20.id, 'tax_dest_id': self.tax20_nd.id})]})
        category = self.env['product.product'].create({
            'name': 'Catégorie non déductible test', 'type': 'service', 'supplier_taxes_id': False})
        category.product_tmpl_id.expense_fiscal_position_id = self.position_nd
        return category

    def _receipt(self):
        type(self).counter += 1
        receipt = self.env['account.move'].create({
            'move_type': 'in_invoice', 'expense_origin': 'employee_receipt', 'partner_id': self.partner.id,
            'invoice_line_ids': [Command.create({
                'name': 'Dépense', 'quantity': 1, 'price_unit': 0.0, 'tax_ids': False,
                'product_id': self.category.id})],
        })
        self.env['ir.attachment'].create({
            'name': 'ticket.png', 'raw': b'ticket-%d' % self.counter, 'mimetype': 'image/png',
            'res_model': 'account.move', 'res_id': receipt.id})
        return receipt

    def _azure(self, data=None, post_status=202):
        post = Mock(status_code=post_status, headers={'Operation-Location': 'https://ocr.example.com/op/1'})
        get = Mock(status_code=200)
        get.json.return_value = data or azure_response()
        self.post_mock = Mock(return_value=post)
        return patch.multiple(REQUESTS, post=self.post_mock, get=Mock(return_value=get))

    def _read(self, data=None):
        receipt = self._receipt()
        with self._azure(data):
            receipt.action_run_ocr()
        return receipt

    # --- service ------------------------------------------------------
    def test_azure_result_is_normalized(self):
        with self._azure(azure_response(rate=10.0)), patch.object(expense_ocr.time, 'sleep'):
            result = self.env['expense.ocr'].analyze(b'x', 'image/png')
        self.assertEqual(result['merchant'], 'TAXI PARISIEN')
        self.assertEqual(result['date'], date(2026, 11, 10))
        self.assertEqual(result['total'], 38.4)
        self.assertEqual(result['tax_total'], 3.49)
        self.assertEqual(result['currency'], 'EUR')
        self.assertEqual(result['lines'], [{'name': 'Course', 'amount': 38.4, 'tax_rate': 10.0}])
        self.assertAlmostEqual(result['confidence'], 0.97)

    def test_service_requires_a_provider(self):
        self.env['ir.config_parameter'].sudo().set_param('expense_core.ocr_provider', '')
        with self.assertRaises(UserError):
            self.env['expense.ocr'].analyze(b'x', 'image/png')
        self.env['ir.config_parameter'].sudo().set_param('expense_core.ocr_provider', 'azure')

    # --- pré-remplissage ----------------------------------------------
    def test_a_rate_read_on_the_line_gives_its_tax_and_the_exact_total(self):
        receipt = self._read(azure_response(38.4, 3.49, rate=10.0))
        line = receipt.invoice_line_ids
        self.assertEqual(receipt.invoice_date, date(2026, 11, 10))
        self.assertEqual(line.tax_ids, self.tax10)
        self.assertEqual(line.name, 'Course')
        self.assertEqual(receipt.amount_total, 38.4)
        self.assertEqual(receipt.amount_tax, 3.49)
        self.assertEqual(receipt.ocr_result_ids.state, 'done')
        self.assertFalse(receipt.expense_warning)

    def test_single_rate_of_the_vat_recap_applies_to_every_line(self):
        receipt = self._read(azure_response(30.0, 5.0, items=[('Plat', 20.0), ('Boisson', 10.0)],
                                            details=[(20.0, 25.0, 5.0)]))
        self.assertEqual(receipt.invoice_line_ids.mapped('tax_ids'), self.tax20)
        self.assertEqual(receipt.invoice_line_ids.mapped('name'), ['Plat', 'Boisson'])
        self.assertEqual(receipt.amount_total, 30.0)
        self.assertEqual(receipt.amount_tax, 5.0)
        self.assertFalse(receipt.expense_warning)

    def test_several_rates_give_one_receipt_line_per_rate(self):
        receipt = self._read(azure_response(
            45.0, 4.85, items=[('Plat', 25.0), ('Dessert', 10.0), ('Livre', 10.0)],
            details=[(10.0, 31.82, 3.18), (20.0, 8.33, 1.67)]))
        lines = receipt.invoice_line_ids
        self.assertEqual(len(lines), 2)
        self.assertEqual({line.tax_ids.amount: line.name for line in lines},
                         {10.0: 'TAXI PARISIEN - TVA 10 %', 20.0: 'TAXI PARISIEN - TVA 20 %'})
        self.assertEqual(receipt.amount_total, 45.0)
        self.assertEqual(receipt.amount_tax, 4.85)
        self.assertFalse(receipt.expense_warning)

    def test_a_vat_recap_not_matching_the_total_is_ignored(self):
        receipt = self._read(azure_response(
            45.0, 4.85, items=[('Plat', 45.0)], details=[(10.0, 20.0, 2.0), (20.0, 8.33, 1.67)]))
        self.assertFalse(receipt.invoice_line_ids.tax_ids)
        self.assertEqual(receipt.amount_total, 45.0)
        self.assertIn('Choisissez le taux', receipt.expense_warning)

    def test_rates_written_as_text_are_understood(self):
        parse = self.env['expense.ocr']._parse_rate
        self.assertEqual(parse({'valueString': '18 %'}), 18.0)
        self.assertEqual(parse({'valueString': '5,5%'}), 5.5)
        self.assertEqual(parse({'valueNumber': 10}), 10.0)
        self.assertIsNone(parse({'valueString': 'exonéré'}))
        self.assertIsNone(parse(None))

    def test_no_rate_is_ever_deduced_from_the_vat_total(self):
        # Même un ticket à un seul taux évident : sans taux lu sur la ligne, le salarié choisit.
        receipt = self._read(azure_response(38.4, 3.49))
        self.assertFalse(receipt.invoice_line_ids.tax_ids)
        self.assertEqual(receipt.amount_total, 38.4)
        self.assertIn('Choisissez le taux de TVA de chaque ligne', receipt.expense_warning)

    def test_one_receipt_line_per_ticket_line_with_the_total_unchanged(self):
        receipt = self._read(azure_response(30.0, 5.0, items=[('Plat', 12.5), ('Dessert', 7.5), ('Boisson', 10.0)]))
        self.assertEqual(receipt.invoice_line_ids.mapped('name'), ['Plat', 'Dessert', 'Boisson'])
        self.assertEqual(receipt.amount_total, 30.0)
        self.assertIn('Choisissez le taux', receipt.expense_warning)

    def test_line_rates_read_on_the_ticket_each_get_their_tax(self):
        data = azure_response(30.0, 4.0, items=[('Plat', 20.0), ('Livre', 10.0)])
        items = data['analyzeResult']['documents'][0]['fields']['Items']['valueArray']
        items[0]['valueObject']['TaxRate'] = {'valueNumber': 10.0}
        items[1]['valueObject']['TaxRate'] = {'valueNumber': 20.0}
        receipt = self._read(data)
        taxes = {line.name: line.tax_ids for line in receipt.invoice_line_ids}
        self.assertEqual(taxes, {'Plat': self.tax10, 'Livre': self.tax20})
        self.assertEqual(receipt.amount_total, 30.0)

    def test_the_banner_follows_the_choices_of_the_user(self):
        receipt = self._read(azure_response(38.4, 3.49))
        self.assertIn('Choisissez le taux', receipt.expense_warning)
        receipt.invoice_line_ids.tax_ids = self.tax10
        self.assertFalse(receipt.expense_warning)
        # Un taux de 20 % sur ce ticket donnerait une TVA de 6,40 : l'écart avec le ticket est signalé.
        receipt.invoice_line_ids.tax_ids = self.tax20
        self.assertIn('TVA de la pièce', receipt.expense_warning)

    def test_ticket_lines_not_matching_the_total_fall_back_to_one_line(self):
        receipt = self._read(azure_response(38.4, 3.49, items=[('Course', 30.0)]))
        self.assertEqual(len(receipt.invoice_line_ids), 1)
        self.assertEqual(receipt.amount_total, 38.4)
        self.assertIn('ne totalisent pas', receipt.expense_warning)

    def test_missing_tax_is_reported(self):
        self.tax10.active = False
        receipt = self._read(azure_response(38.4, 3.49, rate=10.0))
        self.assertFalse(receipt.invoice_line_ids.tax_ids)
        self.assertEqual(receipt.amount_total, 38.4)
        self.assertIn("gestionnaire des notes de frais", receipt.expense_warning)
        self.assertIn("taxe à 10.0 %", receipt.expense_warning)

    def test_a_receipt_without_taxes_is_not_sent_to_validation(self):
        receipt = self._read(azure_response(38.4, 3.49))
        with self.assertRaises(ValidationError):
            receipt._check_expense_lines()
        receipt.invoice_line_ids.tax_ids = self.tax10
        receipt._check_expense_lines()

    def test_a_receipt_without_category_is_not_sent_to_validation(self):
        receipt = self._read(azure_response(38.4, 3.49, rate=10.0))
        receipt._check_expense_lines()
        receipt.expense_category_id = False
        receipt.invoice_line_ids.product_id = False
        receipt.invoice_line_ids.tax_ids = self.tax10
        self.assertIn('Choisissez la catégorie de dépense.', receipt.expense_warning)
        with self.assertRaises(ValidationError):
            receipt._check_expense_lines()

    def test_the_category_is_entered_once_for_every_line(self):
        receipt = self._read(azure_response(30.0, 5.0, items=[('Plat', 20.0), ('Boisson', 10.0)], rate=10.0))
        self.assertEqual(receipt.expense_category_id, self.category)
        other = self.env['product.product'].create({
            'name': 'Catégorie unique', 'type': 'service', 'supplier_taxes_id': False})
        receipt.expense_category_id = other
        self.assertEqual(receipt.invoice_line_ids.mapped('product_id'), other)
        self.assertEqual(receipt.amount_total, 30.0)  # les montants des lignes sont conservés
        self.assertEqual(receipt.invoice_line_ids.mapped('price_unit'), [20.0, 10.0])

    def test_a_line_may_exceptionally_have_another_category(self):
        receipt = self._read(azure_response(30.0, 5.0, items=[('Plat', 20.0), ('Boisson', 10.0)], rate=10.0))
        other = self.env['product.product'].create({
            'name': 'Catégorie exceptionnelle', 'type': 'service', 'supplier_taxes_id': False})
        receipt.invoice_line_ids[1].product_id = other
        receipt.invoice_line_ids.tax_ids = self.tax10
        self.assertFalse(receipt.expense_category_id)  # catégories mélangées : aucune catégorie commune
        receipt._check_expense_lines()  # chaque ligne en a une, la dépense peut partir en validation

    def test_the_category_gives_the_regime_and_the_ticket_keeps_its_rates(self):
        """Ticket de restaurant à 10 % et 20 % : le taux de chaque ligne est celui du ticket, la position fiscale de
        la catégorie donne le régime de TVA (déductible ou non)."""
        category_nd = self._regime_non_deductible()
        data = azure_response(45.0, 4.85, items=[('Menu', 31.82 + 3.18), ('Alcool', 10.0)],
                              details=[(10.0, 31.82, 3.18), (20.0, 8.33, 1.67)])
        receipt = self._read(data)
        initial_position = receipt.fiscal_position_id
        self.assertEqual(sorted(receipt.invoice_line_ids.tax_ids.ids), sorted((self.tax10 | self.tax20).ids))
        self.assertEqual(receipt.amount_total, 45.0)

        receipt.expense_category_id = category_nd
        self.assertEqual(receipt.fiscal_position_id, self.position_nd)
        by_rate = {line.tax_ids.amount: line.tax_ids for line in receipt.invoice_line_ids}
        self.assertEqual(by_rate, {10.0: self.tax10_nd, 20.0: self.tax20_nd})
        self.assertEqual(receipt.amount_total, 45.0)  # les montants et les taux sont conservés
        self.assertEqual(receipt.amount_tax, 4.85)

        # Retour au régime déductible : les taxes de départ reviennent, au même taux.
        receipt.expense_category_id = self.category
        self.assertEqual(receipt.fiscal_position_id, initial_position)  # celle du tiers, comme avant
        by_rate = {line.tax_ids.amount: line.tax_ids for line in receipt.invoice_line_ids}
        self.assertEqual(by_rate, {10.0: self.tax10, 20.0: self.tax20})
        self.assertEqual(receipt.amount_total, 45.0)

    def test_the_category_chosen_before_the_reading_gives_the_same_result(self):
        category_nd = self._regime_non_deductible()
        receipt = self._receipt()
        receipt.expense_category_id = category_nd
        data = azure_response(45.0, 4.85, items=[('Menu', 35.0), ('Alcool', 10.0)],
                              details=[(10.0, 31.82, 3.18), (20.0, 8.33, 1.67)])
        with self._azure(data):
            receipt.action_run_ocr()
        by_rate = {line.tax_ids.amount: line.tax_ids for line in receipt.invoice_line_ids}
        self.assertEqual(by_rate, {10.0: self.tax10_nd, 20.0: self.tax20_nd})
        self.assertEqual(receipt.amount_total, 45.0)

    def test_the_standard_tax_search_ignores_the_taxes_of_the_regimes(self):
        self._regime_non_deductible()
        found = self.env['account.move']._ocr_find_tax(self.company, 10.0)
        # tax10_nd a une séquence plus basse mais n'est que la destination d'une position fiscale de frais
        self.assertEqual(found, self.tax10)

    def test_the_employee_does_not_see_the_lines_unless_it_is_exceptional(self):
        receipt = self._receipt()
        self.assertTrue(receipt.expense_show_lines)  # saisie à la main : pas de lecture, les lignes servent à saisir
        receipt = self._read(azure_response(38.4, 3.49, rate=10.0))
        self.assertFalse(receipt.expense_show_lines)  # lu et complet : lignes masquées
        receipt = self._read(azure_response(30.0, 5.0, items=[('Plat', 20.0), ('Boisson', 10.0)]))
        self.assertTrue(receipt.expense_show_lines)  # taux non lus : les lignes sont à compléter
        receipt.invoice_line_ids.tax_ids = self.tax10
        self.assertFalse(receipt.expense_show_lines)

    def test_a_ticket_with_several_rates_keeps_one_line_per_rate_and_hides_the_lines(self):
        receipt = self._read(azure_response(
            45.0, 4.85, items=[('Menu', 35.0), ('Alcool', 10.0)], details=[(10.0, 31.82, 3.18), (20.0, 8.33, 1.67)]))
        self.assertEqual(len(receipt.invoice_line_ids), 2)
        self.assertEqual(sorted(receipt.invoice_line_ids.mapped('tax_ids.amount')), [10.0, 20.0])
        self.assertFalse(receipt.expense_show_lines)

    def test_existing_values_are_never_overwritten(self):
        receipt = self._receipt()
        receipt.invoice_date = date(2026, 1, 5)
        receipt.invoice_line_ids.price_unit = 12.0
        with self._azure():
            receipt.action_run_ocr()
        self.assertEqual(receipt.invoice_date, date(2026, 1, 5))
        self.assertEqual(receipt.invoice_line_ids.price_unit, 12.0)
        self.assertIn('déjà des lignes', receipt.message_ids[0].body)

    # --- exploitation -------------------------------------------------
    def test_a_file_is_read_only_once(self):
        receipt = self._receipt()
        with self._azure():
            receipt.action_run_ocr()
            receipt.action_run_ocr()
        self.assertEqual(len(receipt.ocr_result_ids), 1)
        self.assertEqual(self.post_mock.call_count, 1)

    def test_a_failing_service_leaves_the_receipt_untouched(self):
        receipt = self._receipt()
        with self._azure(post_status=500):
            receipt.action_run_ocr()
        self.assertEqual(receipt.ocr_result_ids.state, 'error')
        self.assertTrue(receipt.ocr_result_ids.error)
        self.assertFalse(receipt.invoice_date)
        self.assertEqual(receipt.invoice_line_ids.price_unit, 0.0)

    def test_cron_reads_pending_receipts(self):
        receipt = self._receipt()
        with self._azure():
            moves = self.env['account.move']._cron_run_ocr()
        self.assertIn(receipt, moves)
        self.assertEqual(receipt.ocr_result_ids.state, 'done')

    def test_nothing_runs_without_a_configured_service(self):
        self.env['ir.config_parameter'].sudo().set_param('expense_core.ocr_provider', '')
        receipt = self._receipt()
        with self._azure():
            receipt.action_run_ocr()
            self.assertFalse(self.env['account.move']._cron_run_ocr())
        self.assertFalse(receipt.ocr_result_ids)
        self.assertEqual(self.post_mock.call_count, 0)
        self.env['ir.config_parameter'].sudo().set_param('expense_core.ocr_provider', 'azure')
