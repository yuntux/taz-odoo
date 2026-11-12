from datetime import date

from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

EMPLOYEE_TEST_IBAN = 'FR1420041010050500013M02606'
QONTO_TEST_IBAN = 'FR7630006000011234567890189'


@tagged('post_install', '-at_install')
class TestEmployeeReceipt(TransactionCase):
    """Lot 2 : reçu d'un salarié (employé, projet du staffing, IBAN) et remboursement hebdomadaire."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        ref = cls.env.ref
        cls.user = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Consultant test', 'login': 'consultant_test', 'email': 'consultant@example.com',
            'groups_id': [Command.set([ref('base.group_user').id, ref('account.group_account_invoice').id])],
        })
        cls.office = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Office test', 'login': 'office_rcpt', 'email': 'office_rcpt@example.com',
            'groups_id': [Command.set([ref('base.group_user').id, ref('account.group_account_invoice').id,
                                       ref('taz-common.taz-adv').id])],
        })
        cls.employee = cls.env['hr.employee'].create({'name': 'Consultant test', 'user_id': cls.user.id})
        cls.projects = cls.env['project.project'].search(
            [('account_id', '!=', False), ('company_id', '=', cls.company.id)], limit=2)
        if len(cls.projects) < 2:
            cls.skipTest(cls, "il faut deux projets avec compte analytique dans la base")
        cls.staffed, cls.other = cls.projects
        need = cls.env['staffing.need'].create({
            'project_id': cls.staffed.id, 'staffed_employee_id': cls.employee.id, 'company_id': cls.company.id,
        })
        # Les dates d'un besoin sont calculées depuis les prévisions : on les fixe directement pour le test.
        cls.env.flush_all()
        cls.env.cr.execute(
            "UPDATE staffing_need SET begin_date = %s, end_date = %s WHERE id = %s",
            (date(2026, 1, 1), date(2026, 12, 31), need.id))
        cls.env.invalidate_all()
        # project_accounting n'accepte sur un projet que des écritures de son client ou de ses fournisseurs
        # déclarés (onglet Achats) : le salarié y est déclaré, avec le type « autres ».
        for project in cls.projects:
            cls.env['project.outsourcing.link'].create({
                'project_id': project.id, 'partner_id': cls.employee.work_contact_id.id, 'link_type': 'other',
                'company_id': cls.company.id})

    def _receipt(self, **extra):
        vals = {
            'move_type': 'in_invoice', 'expense_origin': 'employee_receipt', 'partner_id': self.employee.work_contact_id.id, 'invoice_date': '2026-11-10',
            'invoice_line_ids': [Command.create({'name': 'Taxi', 'quantity': 1, 'price_unit': 38.4})],
        }
        vals.update(extra)
        return self.env['account.move'].create(vals)

    def test_employee_fills_payee_partner_and_bank_account(self):
        bank = self.env['res.partner.bank'].create({
            'acc_number': EMPLOYEE_TEST_IBAN, 'partner_id': self.employee.work_contact_id.id})
        self.employee.bank_account_id = bank
        receipt = self._receipt()
        self.assertEqual(receipt._get_expense_employee(), self.employee)
        self.assertEqual(receipt.partner_id, self.employee.work_contact_id)
        self.assertEqual(receipt.partner_bank_id, bank)

    def test_merchant_is_kept_apart_from_the_accounting_partner(self):
        merchant = self.env['res.partner'].create({'name': 'Taxi Parisien SARL', 'is_company': True})
        receipt = self._receipt(expense_merchant_id=merchant.id)
        self.assertEqual(receipt.partner_id, self.employee.work_contact_id)
        self.assertEqual(receipt.expense_merchant_id, merchant)

    def test_project_supplier_may_be_a_company_or_an_employee(self):
        from ast import literal_eval
        domain = literal_eval(self.env['project.outsourcing.link']._fields['partner_id'].domain)
        partners = self.env['res.partner'].search(domain)
        self.assertIn(self.employee.work_contact_id, partners)
        company = self.env['res.partner'].create({'name': 'Société quelconque', 'is_company': True})
        person = self.env['res.partner'].create({'name': 'Personne sans fiche salarié', 'is_company': False})
        partners = self.env['res.partner'].search(domain)
        self.assertIn(company, partners)
        self.assertNotIn(person, partners)

    def test_receipt_defaults_to_the_employee_of_the_user(self):
        receipt = self.env['account.move'].with_user(self.user).create({
            'move_type': 'in_invoice', 'expense_origin': 'employee_receipt', 'invoice_date': '2026-11-10',
            'invoice_line_ids': [Command.create({'name': 'Taxi', 'quantity': 1, 'price_unit': 12})],
        })
        self.assertEqual(receipt.partner_id, self.employee.work_contact_id)
        self.assertEqual(receipt._get_expense_employee(), self.employee)

    def test_project_choice_sets_the_analytic_distribution(self):
        receipt = self._receipt()
        receipt.expense_project_id = self.staffed
        self.assertEqual(receipt.line_ids.rel_project_ids, self.staffed)
        self.assertEqual(receipt.invoice_line_ids.analytic_distribution, {str(self.staffed.account_id.id): 100})
        self.assertEqual(receipt.expense_project_id, self.staffed)

    def test_staffed_projects_depend_on_the_expense_date(self):
        receipt = self._receipt()
        self.assertIn(self.staffed, receipt.allowed_project_ids)
        self.assertNotIn(self.other, receipt.allowed_project_ids)
        receipt.invoice_date = date(2025, 6, 1)
        self.assertNotIn(self.staffed, receipt.allowed_project_ids)

    def test_a_posted_expense_counts_in_the_cost_of_the_project(self):
        """Raison du choix de in_invoice : un reçu (in_receipt) serait absent des coûts du projet."""
        receipt = self._receipt()
        receipt.invoice_line_ids.write({'price_unit': 38.4, 'tax_ids': False})
        receipt.expense_project_id = self.staffed
        receipt.action_post()
        link = self.env['project.outsourcing.link'].search([
            ('project_id', '=', self.staffed.id), ('partner_id', '=', self.employee.work_contact_id.id)])
        self.staffed.compute()
        link.invalidate_recordset()
        self.assertAlmostEqual(link.sum_account_move_lines, 38.4)

    def test_choosing_an_allowed_project_declares_the_employee_as_supplier(self):
        newcomer_user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Nouveau salarié', 'login': 'newcomer_rcpt', 'email': 'newcomer@example.com',
            'groups_id': [Command.set([self.env.ref('base.group_user').id, self.env.ref('taz-common.taz-basic').id])]})
        newcomer = self.env['hr.employee'].create({'name': 'Nouveau salarié', 'user_id': newcomer_user.id})
        need = self.env['staffing.need'].create({
            'project_id': self.staffed.id, 'staffed_employee_id': newcomer.id, 'company_id': self.company.id})
        self.env.flush_all()
        self.env.cr.execute("UPDATE staffing_need SET begin_date=%s, end_date=%s WHERE id=%s",
                            (date(2026, 1, 1), date(2026, 12, 31), need.id))
        self.env.invalidate_all()
        Link = self.env['project.outsourcing.link']
        domain = [('project_id', '=', self.staffed.id), ('partner_id', '=', newcomer.work_contact_id.id)]
        self.assertFalse(Link.search(domain))
        receipt = self.env['account.move'].create({
            'move_type': 'in_invoice', 'expense_origin': 'employee_receipt', 'partner_id': newcomer.work_contact_id.id, 'invoice_date': '2026-11-10',
            'invoice_line_ids': [Command.create({'name': 'Taxi', 'quantity': 1, 'price_unit': 10.0})]})
        receipt.with_user(newcomer_user).expense_project_id = self.staffed
        link = Link.search(domain)
        self.assertEqual(link.link_type, 'other')
        self.assertEqual(receipt.line_ids.rel_project_ids, self.staffed)
        # Choisir deux fois le même projet ne crée pas un second lien.
        receipt.with_user(newcomer_user).expense_project_id = self.staffed
        self.assertEqual(Link.search_count(domain), 1)

    def test_a_project_the_employee_may_not_choose_gets_no_link(self):
        outsider = self.env['hr.employee'].create({'name': 'Salarié non staffé', 'work_contact_id': self.env['res.partner'].create({'name': 'Salarié non staffé'}).id})
        receipt = self.env['account.move'].create({
            'move_type': 'in_invoice', 'expense_origin': 'employee_receipt', 'partner_id': outsider.work_contact_id.id, 'invoice_date': '2026-11-10',
            'invoice_line_ids': [Command.create({'name': 'Taxi', 'quantity': 1, 'price_unit': 10.0})]})
        with self.assertRaises(ValidationError):
            receipt.with_user(self.user).expense_project_id = self.staffed
        self.assertFalse(self.env['project.outsourcing.link'].search_count(
            [('project_id', '=', self.staffed.id), ('partner_id', '=', outsider.work_contact_id.id)]))

    def test_card_expense_declares_the_real_merchant_as_supplier_of_the_project(self):
        merchant = self.env['res.partner'].create({'name': 'Restaurant Le Bon Coin', 'is_company': True})
        Link = self.env['project.outsourcing.link']
        domain = [('project_id', '=', self.staffed.id), ('partner_id', '=', merchant.id)]
        self.assertFalse(Link.search(domain))
        card = self.env['account.move'].create({
            'move_type': 'in_invoice', 'expense_origin': 'card', 'partner_id': merchant.id, 'invoice_date': '2026-11-10',
            'invoice_line_ids': [Command.create({'name': 'Déjeuner', 'quantity': 1, 'price_unit': 45.0})]})
        card.with_user(self.user).expense_project_id = self.staffed
        self.assertEqual(Link.search(domain).link_type, 'other')
        self.assertEqual(card.line_ids.rel_project_ids, self.staffed)
        # Le salarié n'est pas déclaré à la place du commerçant, et un projet non staffé reste refusé.
        self.assertFalse(Link.search_count([
            ('project_id', '=', self.staffed.id), ('partner_id', '=', self.employee.work_contact_id.id),
            ('create_uid', '=', self.user.id)]))
        other_merchant = self.env['res.partner'].create({'name': 'Hôtel Lointain', 'is_company': True})
        card2 = self.env['account.move'].create({
            'move_type': 'in_invoice', 'expense_origin': 'card', 'partner_id': other_merchant.id, 'invoice_date': '2026-11-10',
            'invoice_line_ids': [Command.create({'name': 'Nuit', 'quantity': 1, 'price_unit': 90.0})]})
        with self.assertRaises(ValidationError):
            card2.with_user(self.user).expense_project_id = self.other

    def test_the_director_of_a_project_may_charge_it_without_being_staffed(self):
        receipt = self._receipt()
        self.assertNotIn(self.other, receipt.allowed_project_ids)
        self.other.project_director_employee_id = self.employee
        self.assertEqual(self.other.user_id, self.employee.user_id)
        receipt.invalidate_recordset(['allowed_project_ids'])
        self.assertIn(self.other, receipt.allowed_project_ids)
        receipt.expense_project_id = self.other
        receipt.with_user(self.user)._check_expense_staffing()

    def test_project_outside_the_staffing_is_refused_except_for_the_office(self):
        receipt = self._receipt()
        receipt.expense_project_id = self.other
        with self.assertRaises(ValidationError):
            receipt.with_user(self.user)._check_expense_staffing()
        receipt.with_user(self.office)._check_expense_staffing()  # l'Office peut corriger l'imputation
        receipt.expense_project_id = self.staffed
        receipt.with_user(self.user)._check_expense_staffing()

    def test_weekly_reimbursement_order(self):
        qonto = self.env['res.partner.bank'].create({
            'acc_number': QONTO_TEST_IBAN, 'partner_id': self.company.partner_id.id,
            'bank_id': self.env['res.bank'].create({'name': 'Qonto (test)', 'bic': 'QNTOFRP1XXX'}).id})
        journal = self.env['account.journal'].create({
            'name': 'Qonto (test)', 'type': 'bank', 'code': 'QNTR', 'bank_account_id': qonto.id})
        method = self.env.ref('account_payment_sepa_credit_transfer.sepa_credit_transfer')
        line = journal.outbound_payment_method_line_ids.filtered(
            lambda l: l.payment_method_id == method)[:1] or self.env['account.payment.method.line'].create({
                'name': 'Virement SEPA remboursements', 'journal_id': journal.id,
                'payment_method_id': method.id, 'selectable': True})
        self.company.expense_reimbursement_method_line_id = line

        bank = self.env['res.partner.bank'].create({
            'acc_number': EMPLOYEE_TEST_IBAN, 'partner_id': self.employee.work_contact_id.id})
        self.employee.bank_account_id = bank
        trusted = self._receipt()
        no_bank = self._receipt(partner_bank_id=False)
        no_bank.partner_bank_id = False
        for receipt in trusted | no_bank:
            receipt.action_post()
        # Le compte du salarié n'est pas encore de confiance : rien n'est ajouté.
        self.assertFalse(self.env['account.move']._cron_create_reimbursement_order())
        bank.allow_out_payment = True
        added = self.env['account.move']._cron_create_reimbursement_order()
        self.assertEqual(added, trusted)
        self.env.invalidate_all()
        self.assertEqual(trusted.payment_line_count, 1)
        self.assertFalse(no_bank.payment_line_count)
        # Une seconde exécution n'ajoute pas deux fois le même reçu.
        self.env.invalidate_all()
        self.assertFalse(self.env['account.move']._cron_create_reimbursement_order())
