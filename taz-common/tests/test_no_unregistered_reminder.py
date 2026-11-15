from datetime import datetime, timedelta

from odoo import Command, fields
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestNoUnregisteredUserReminder(TransactionCase):
    """Plus de mail « Invitation en attente » au créateur d'un compte jamais utilisé."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        users = cls.env['res.users'].with_context(no_reset_password=True)
        cls.creator = users.create({
            'name': 'Créateur de comptes', 'login': 'createur_comptes', 'email': 'createur_comptes@example.com',
            'groups_id': [Command.set([cls.env.ref('base.group_system').id])]})
        cls.invited = users.create({
            'name': 'Invité jamais connecté', 'login': 'invite_jamais', 'email': 'invite_jamais@example.com',
            'groups_id': [Command.set([cls.env.ref('base.group_user').id])]})

    def test_the_reminder_sends_nothing(self):
        # compte créé par `creator` il y a 5 jours : dans la fenêtre que le rappel d'Odoo cherche
        five_days_ago = datetime.combine(fields.Date.today(), datetime.min.time()) - timedelta(days=5) + timedelta(hours=1)
        self.env.cr.execute(
            "UPDATE res_users SET create_date = %s, create_uid = %s WHERE id = %s",
            (five_days_ago, self.creator.id, self.invited.id))
        self.invited.invalidate_recordset(['create_date', 'create_uid'])
        before = self.env['mail.mail'].sudo().search_count([])
        self.assertIsNone(self.env['res.users'].send_unregistered_user_reminder())
        self.assertEqual(self.env['mail.mail'].sudo().search_count([]), before)
