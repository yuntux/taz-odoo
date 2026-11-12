from datetime import date
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPurchaseOrderReconcile(TransactionCase):
    """À la comptabilisation, une dépense est rapprochée du BC du salarié sur le projet, comme par l'Office."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.employee = cls.env['hr.employee'].create({'name': 'Salarié BC test'})
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
        cls.category = cls.env['product.product'].create({
            'name': 'Catégorie BC test', 'type': 'service', 'purchase_ok': True, 'supplier_taxes_id': False})
        cls.tax10 = cls.env['account.tax'].create({
            'name': 'TVA 10% TTC BC test', 'amount': 10, 'price_include_override': 'tax_included',
            'type_tax_use': 'purchase', 'company_id': cls.company.id})

    def _expense(self, amount=38.4, project=True):
        move = self.env['account.move'].create({
            'move_type': 'in_invoice', 'expense_origin': 'employee_receipt',
            'partner_id': self.employee.work_contact_id.id, 'invoice_date': '2026-11-10',
            'invoice_line_ids': [Command.create({
                'name': 'Dépense', 'quantity': 1, 'price_unit': amount, 'product_id': self.category.id,
                'tax_ids': [Command.set(self.tax10.ids)]})],
        })
        if project:
            move.expense_project_id = self.project
        return move

    def _employee_orders(self):
        return self.env['purchase.order'].search([('partner_id', '=', self.employee.work_contact_id.id)])

    def _envelope(self, amount=1000.0):
        """Enveloppe « frais divers » du projet : BC d'un fournisseur fictif, désigné dans la société."""
        partner = self.env['res.partner'].create({'name': 'Frais divers test', 'is_company': True})
        self.company.expense_envelope_partner_id = partner
        self.env['project.outsourcing.link'].create({
            'project_id': self.project.id, 'partner_id': partner.id, 'link_type': 'other',
            'company_id': self.company.id})
        order = self.env['purchase.order'].create({
            'partner_id': partner.id,
            'order_line': [Command.create({
                'product_id': self.category.id, 'name': 'Frais prévus', 'product_qty': 1, 'price_unit': amount,
                'analytic_distribution': {str(self.project.account_id.id): 100}})]})
        order.button_confirm()
        return order

    def test_first_expense_creates_the_order_of_the_employee_on_the_project(self):
        expense = self._expense()
        self.assertFalse(self._employee_orders())
        expense.action_post()
        orders = self._employee_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders.state, 'purchase')
        line = expense.invoice_line_ids
        self.assertEqual(line.purchase_line_id.order_id, orders)
        self.assertEqual(line.purchase_line_id.expense_move_id, expense)
        self.assertAlmostEqual(orders.amount_untaxed, expense.amount_untaxed)

    def test_later_expenses_are_added_to_the_same_order(self):
        first, second = self._expense(38.4), self._expense(22.0)
        first.action_post()
        second.action_post()
        orders = self._employee_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(orders.order_line.filtered(lambda l: not l.display_type)), 2)
        self.assertAlmostEqual(orders.amount_untaxed, first.amount_untaxed + second.amount_untaxed)
        self.assertEqual(second.invoice_line_ids.purchase_line_id.order_id, orders)

    def test_the_miscellaneous_expenses_envelope_is_consumed(self):
        envelope = self._envelope(1000.0)
        expense = self._expense(38.4)
        expense.action_post()
        self.assertAlmostEqual(envelope.amount_untaxed, 1000.0 - expense.amount_untaxed)

    def test_cancelling_the_expense_undoes_the_reconciliation(self):
        envelope = self._envelope(1000.0)
        expense = self._expense(38.4)
        expense.action_post()
        order = self._employee_orders()
        self.assertTrue(order.order_line)
        expense.button_draft()
        expense.button_cancel()
        self.assertFalse(order.order_line.filtered(lambda l: not l.display_type))
        self.assertEqual(order.state, 'cancel')
        self.assertFalse(expense.invoice_line_ids.purchase_line_id)
        self.assertAlmostEqual(envelope.amount_untaxed, 1000.0)

    def test_a_card_expense_is_reconciled_with_the_order_of_the_real_supplier(self):
        """Pour une carte, le tiers de la pièce est le commerçant : le BC cherché ou créé est le sien, pas celui du
        porteur de la carte."""
        merchant = self.env['res.partner'].create({'name': 'Brasserie test', 'is_company': True})
        self.env['project.outsourcing.link'].create({
            'project_id': self.project.id, 'partner_id': merchant.id, 'link_type': 'other',
            'company_id': self.company.id})
        vals = {
            'move_type': 'in_invoice', 'expense_origin': 'card', 'partner_id': merchant.id,
            'invoice_date': '2026-11-10',
            'invoice_line_ids': [Command.create({
                'name': 'Repas', 'quantity': 1, 'price_unit': 42.1, 'product_id': self.category.id,
                'tax_ids': [Command.set(self.tax10.ids)]})]}
        first, second = self.env['account.move'].create([vals, dict(vals)])
        for card_expense in first | second:
            card_expense.expense_project_id = self.project
            card_expense.action_post()
        orders = self.env['purchase.order'].search([('partner_id', '=', merchant.id)])
        self.assertEqual(len(orders), 1)  # cumulatif : une seule commande pour le commerçant sur le projet
        self.assertEqual(len(orders.order_line.filtered(lambda l: not l.display_type)), 2)
        self.assertFalse(self._employee_orders())  # rien n'est créé au nom d'un salarié

    def test_an_expense_without_project_is_left_alone(self):
        expense = self._expense(project=False)
        expense.action_post()
        self.assertFalse(self._employee_orders())

    def test_a_reconciliation_failure_never_blocks_the_posting(self):
        expense = self._expense()
        model = 'odoo.addons.l10n_fr_einvoicing_extra.wizards.bill_po_reconcile.BillPoReconcileWizard.action_associate'
        with patch(model, side_effect=UserError("échec simulé")):
            expense.action_post()
        self.assertEqual(expense.state, 'posted')
        self.assertFalse(self._employee_orders())
        self.assertIn("Rapprochement avec un bon de commande impossible", expense.message_ids[0].body)
