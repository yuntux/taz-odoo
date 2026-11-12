from datetime import date
from unittest.mock import Mock, patch

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from .test_ocr import REQUESTS, azure_response


@tagged('post_install', '-at_install')
class TestEmployeeFlow(TransactionCase):
    """Parcours d'un consultant ordinaire (groupe TAZ-Basic) : saisir son reçu, le faire lire, l'envoyer en validation."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        ref = cls.env.ref
        groups = [ref('base.group_user'), ref('taz-common.taz-basic')]
        cls.consultant = cls._user('consultant_flow', groups)
        cls.colleague = cls._user('colleague_flow', groups)
        cls.outsider = cls._user('outsider_flow', [ref('base.group_user')])
        cls.employee = cls.env['hr.employee'].create({'name': 'Consultant flow', 'user_id': cls.consultant.id})
        cls.colleague_employee = cls.env['hr.employee'].create({'name': 'Collègue flow', 'user_id': cls.colleague.id})
        cls.project = cls.env['project.project'].search(
            [('account_id', '!=', False), ('company_id', '=', cls.company.id)], limit=1)
        if not cls.project:
            cls.skipTest(cls, "il faut un projet avec compte analytique dans la base")
        need = cls.env['staffing.need'].create({
            'project_id': cls.project.id, 'staffed_employee_id': cls.employee.id, 'company_id': cls.company.id})
        cls.env.flush_all()
        cls.env.cr.execute("UPDATE staffing_need SET begin_date=%s, end_date=%s WHERE id=%s",
                           (date(2026, 1, 1), date(2026, 12, 31), need.id))
        cls.env.invalidate_all()
        # Aucun lien salarié-projet n'est déclaré à la main : il est créé au choix du projet.
        cls.category = cls.env['product.product'].create({
            'name': 'Catégorie flow', 'type': 'service', 'supplier_taxes_id': False})
        cls.tax10 = cls.env['account.tax'].create({
            'name': 'TVA 10% TTC flow', 'amount': 10, 'price_include_override': 'tax_included',
            'type_tax_use': 'purchase', 'company_id': cls.company.id})
        params = cls.env['ir.config_parameter'].sudo()
        params.set_param('expense_core.ocr_provider', 'azure')
        params.set_param('expense_core.ocr_endpoint', 'https://ocr.example.com')
        params.set_param('expense_core.ocr_api_key', 'cle')

    @classmethod
    def _user(cls, login, groups):
        return cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': login, 'login': login, 'email': f'{login}@example.com',
            'groups_id': [Command.set([g.id for g in groups])]})

    def _receipt_as_consultant(self):
        return self.env['account.move'].with_user(self.consultant).create({
            'move_type': 'in_invoice', 'expense_origin': 'employee_receipt', 'invoice_date': '2026-11-10',
            'invoice_line_ids': [Command.create({
                'name': 'Dépense', 'quantity': 1, 'price_unit': 0.0, 'product_id': self.category.id})],
        })

    def test_consultant_creates_his_own_receipt(self):
        receipt = self._receipt_as_consultant()
        self.assertEqual(receipt.partner_id, self.employee.work_contact_id)
        self.assertEqual(receipt.state, 'draft')

    def test_consultant_cannot_touch_the_receipt_of_a_colleague(self):
        other = self.env['account.move'].create({
            'move_type': 'in_invoice', 'expense_origin': 'employee_receipt', 'partner_id': self.colleague_employee.work_contact_id.id,
            'invoice_line_ids': [Command.create({'name': 'Taxi', 'quantity': 1, 'price_unit': 10.0})]})
        with self.assertRaises(AccessError):
            other.with_user(self.consultant).write({'ref': 'modifié'})

    def test_consultant_cannot_create_a_supplier_invoice(self):
        with self.assertRaises(AccessError):
            self.env['account.move'].with_user(self.consultant).create({
                'move_type': 'in_invoice', 'partner_id': self.employee.work_contact_id.id,
                'invoice_line_ids': [Command.create({'name': 'Facture', 'quantity': 1, 'price_unit': 10.0})]})

    def test_whole_flow_from_the_consultant(self):
        (self.env.ref('expense_core.tier_definition_bap_office')
         | self.env.ref('expense_core.tier_definition_bap_director')).write({'active': True})
        receipt = self._receipt_as_consultant()
        self.env['ir.attachment'].with_user(self.consultant).create({
            'name': 'ticket.png', 'raw': b'ticket-flow', 'mimetype': 'image/png',
            'res_model': 'account.move', 'res_id': receipt.id})
        post = Mock(status_code=202, headers={'Operation-Location': 'https://ocr.example.com/op'})
        get = Mock(status_code=200)
        get.json.return_value = azure_response(38.4, 3.49, details=[(10.0, 34.91, 3.49)])
        with patch.multiple(REQUESTS, post=Mock(return_value=post), get=Mock(return_value=get)):
            receipt.with_user(self.consultant).action_run_ocr()
        self.assertEqual(receipt.ocr_result_ids.state, 'done')
        self.assertEqual(receipt.invoice_line_ids.tax_ids, self.tax10)
        self.assertEqual(receipt.amount_total, 38.4)

        receipt.with_user(self.consultant).expense_project_id = self.project
        self.assertEqual(receipt.line_ids.rel_project_ids, self.project)
        receipt.with_user(self.consultant).request_validation()
        self.env.invalidate_all()
        self.assertEqual(receipt.validation_status, 'pending')
        # Le consultant ne peut ni comptabiliser, ni modifier la pièce pendant la validation.
        with self.assertRaises(Exception):  # noqa: B017 - refus de validation ou de droit, selon la couche
            receipt.with_user(self.consultant).action_post()
        self.assertEqual(receipt.state, 'draft')

    def test_a_receipt_outside_the_staffing_is_refused_at_submission(self):
        receipt = self._receipt_as_consultant()
        other_project = self.env['project.project'].search(
            [('account_id', '!=', False), ('company_id', '=', self.company.id), ('id', '!=', self.project.id)], limit=1)
        self.env['project.outsourcing.link'].create({
            'project_id': other_project.id, 'partner_id': self.employee.work_contact_id.id, 'link_type': 'other',
            'company_id': self.company.id})
        receipt.expense_project_id = other_project
        receipt.invoice_line_ids.tax_ids = self.tax10
        with self.assertRaises(ValidationError):
            receipt.with_user(self.consultant).request_validation()

    def test_simplified_form_and_menu_load(self):
        form = self.env.ref('expense_core.view_expense_receipt_form')
        arch = self.env['account.move'].get_view(form.id)['arch']
        arch = arch if isinstance(arch, str) else arch.decode()
        self.assertIn('expense_project_id', arch)
        self.assertIn('allowed_project_ids', arch)
        action = self.env.ref('expense_core.action_expense_my_receipts')
        self.assertEqual(action.res_model, 'account.move')
        menu = self.env.ref('expense_core.menu_expense_root')
        self.assertIn(menu.id, self.env['ir.ui.menu'].with_user(self.consultant)._visible_menu_ids())
        self.assertNotIn(menu.id, self.env['ir.ui.menu'].with_user(self.outsider)._visible_menu_ids())
