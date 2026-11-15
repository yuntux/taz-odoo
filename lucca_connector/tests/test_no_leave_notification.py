from datetime import date

from odoo import Command
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestNoLeaveNotification(TransactionCase):
    """Les congés sont gérés dans Lucca : leurs messages restent dans l'historique, personne n'est notifié."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=False, no_reset_password=True))
        cls.manager_user = cls.env['res.users'].create({
            'name': 'Responsable congés', 'login': 'resp_conges', 'email': 'resp_conges@example.com',
            'groups_id': [Command.set([cls.env.ref('base.group_user').id])]})
        cls.user = cls.env['res.users'].create({
            'name': 'Salarié congés', 'login': 'salarie_conges', 'email': 'salarie_conges@example.com',
            'groups_id': [Command.set([cls.env.ref('base.group_user').id])]})
        manager = cls.env['hr.employee'].create({'name': 'Responsable congés', 'user_id': cls.manager_user.id})
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Salarié congés', 'user_id': cls.user.id, 'leave_manager_id': cls.manager_user.id,
            'parent_id': manager.id})
        leave_type = cls.env['hr.leave.type'].create({
            'name': 'Type sans allocation (test)', 'requires_allocation': 'no', 'leave_validation_type': 'no_validation'})
        cls.leave = cls.env['hr.leave'].sudo().create({
            'employee_id': cls.employee.id, 'holiday_status_id': leave_type.id,
            'request_date_from': date(2026, 12, 7), 'request_date_to': date(2026, 12, 8)})

    def _notifications(self):
        return self.env['mail.notification'].sudo().search([('mail_message_id.model', '=', 'hr.leave')])

    def test_a_message_posted_on_a_leave_notifies_nobody_but_stays_in_the_history(self):
        before = self._notifications()
        message = self.leave.message_post(
            body="Message de test", message_type='comment', subtype_xmlid='mail.mt_comment',
            partner_ids=self.manager_user.partner_id.ids)
        self.assertIn(message, self.leave.message_ids)
        self.assertEqual(self._notifications(), before)

    def test_the_change_notice_stays_in_the_history_and_the_employee_is_not_notified(self):
        before = self._notifications()
        self.leave._notify_change("Votre congé a changé")
        self.assertIn("Votre congé a changé", ' '.join(self.leave.message_ids.mapped('body')))
        self.assertEqual(self._notifications(), before)
        self.assertFalse(self.env['mail.notification'].sudo().search([
            ('res_partner_id', '=', self.user.partner_id.id), ('mail_message_id.subject', 'ilike', 'Time Off')]))

    def test_the_cancellation_does_not_notify_the_responsible(self):
        before = self.env['mail.notification'].sudo().search_count([])
        self.leave._force_cancel("Annulé dans Lucca")
        self.assertIn("Annulé dans Lucca", ' '.join(self.leave.message_ids.mapped('body')))
        self.assertEqual(self.env['mail.notification'].sudo().search_count([]), before)

    def test_the_refusal_notice_to_the_manager_is_gone(self):
        before = self.env['mail.notification'].sudo().search_count([])
        self.leave._notify_manager()
        self.assertEqual(self.env['mail.notification'].sudo().search_count([]), before)

    def test_other_models_still_notify(self):
        """La coupure ne concerne que les congés : un message adressé sur une autre fiche notifie toujours."""
        partner = self.env['res.partner'].create({'name': 'Fiche témoin'})
        self.manager_user.notification_type = 'inbox'
        partner.message_post(
            body="Témoin", message_type='comment', subtype_xmlid='mail.mt_comment',
            partner_ids=self.manager_user.partner_id.ids)
        self.assertTrue(self.env['mail.notification'].sudo().search([
            ('res_partner_id', '=', self.manager_user.partner_id.id), ('mail_message_id.model', '=', 'res.partner')]))
