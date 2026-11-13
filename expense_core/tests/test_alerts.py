from odoo import Command
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestExpenseAlerts(TransactionCase):
    """Alertes de seuil : dépense au-dessus de 500 €, cumul d'une personne au-dessus de 1 500 € sur 30 jours."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.write({'expense_alert_amount': 500.0, 'expense_cumul_amount': 1500.0, 'expense_cumul_days': 30})
        mk = lambda login: cls.env['res.users'].with_context(no_reset_password=True).create(
            {'name': login, 'login': login, 'email': login + '@example.com',
             'groups_id': [Command.set([cls.env.ref('base.group_user').id])]})
        cls.manager_user, cls.boss_user = mk('mgr_alert'), mk('boss_alert')
        cls.boss_user.groups_id = [Command.link(cls.env.ref('taz-common.taz-management').id)]
        cls.manager = cls.env['hr.employee'].create({'name': 'Manager', 'user_id': cls.manager_user.id})
        cls.employee = cls.env['hr.employee'].create({'name': 'Consultant alerte', 'parent_id': cls.manager.id})

    def _receipt(self, amount, date='2026-11-10'):
        return self.env['account.move'].create({
            'move_type': 'in_invoice', 'expense_origin': 'employee_receipt',
            'partner_id': self.employee.work_contact_id.id, 'invoice_date': date,
            'invoice_line_ids': [Command.create({'name': 'Frais', 'quantity': 1, 'price_unit': amount})]})

    def _alerts(self, move, subject):
        return self.env['mail.message'].search([
            ('model', '=', 'account.move'), ('res_id', '=', move.id), ('subject', 'ilike', subject)])

    def test_no_alert_below_the_threshold(self):
        move = self._receipt(100.0)
        move._expense_send_alerts()
        self.assertFalse(self._alerts(move, 'à valider'))

    def test_expense_above_the_threshold_goes_to_the_management_without_project(self):
        move = self._receipt(600.0)
        move._expense_send_alerts()
        alert = self._alerts(move, 'à valider')
        self.assertIn(self.boss_user.partner_id, alert.partner_ids)

    def test_cumul_alert_goes_to_the_manager_once_when_crossed(self):
        for _i in range(3):
            move = self._receipt(450.0)
            move._expense_send_alerts()
            self.assertFalse(self._alerts(move, 'Cumul'), "1 350 € au plus : seuil non franchi")
        crossing = self._receipt(450.0)  # 1 800 € en tout : franchi par cette pièce
        crossing._expense_send_alerts()
        alert = self._alerts(crossing, 'Cumul')
        self.assertEqual(alert.partner_ids, self.manager_user.partner_id)
        after = self._receipt(10.0)
        after._expense_send_alerts()
        self.assertFalse(self._alerts(after, 'Cumul'), "pas de nouvelle alerte une fois le seuil déjà franchi")


@tagged('post_install', '-at_install')
class TestNotificationPreference(TransactionCase):

    def _user(self, login, **extra):
        vals = {'name': login, 'login': login, 'email': login + '@example.com'}
        vals.update(extra)
        return self.env['res.users'].with_context(no_reset_password=True).create(vals)

    def test_new_internal_user_gets_notifications_in_odoo_for_push(self):
        self.assertEqual(self._user('push_default').notification_type, 'inbox')

    def test_explicit_choice_is_respected(self):
        self.assertEqual(self._user('push_email', notification_type='email').notification_type, 'email')

    def test_portal_user_stays_on_email(self):
        user = self._user('push_portal', groups_id=[Command.set([self.env.ref('base.group_portal').id])])
        self.assertEqual(user.notification_type, 'email')
