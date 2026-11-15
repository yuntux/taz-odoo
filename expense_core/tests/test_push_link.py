import base64
import io
import json
from unittest.mock import patch

from PIL import Image

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged


def _png_b64():
    buffer = io.BytesIO()
    Image.new('RGB', (40, 60), (255, 255, 255)).save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode()


class CardExpenseCase(TransactionCase):
    """Une dépense par carte en brouillon, dont le porteur est l'employé d'un utilisateur ordinaire (TAZ-Basic)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        ref = cls.env.ref
        groups = [ref('base.group_user'), ref('taz-common.taz-basic')]
        cls.holder = cls._user('porteur_push', groups)
        cls.other = cls._user('autre_push', groups)
        cls.employee = cls.env['hr.employee'].create({'name': 'Porteur push', 'user_id': cls.holder.id})
        cls.merchant = cls.env['res.partner'].create({'name': 'Commerçant push', 'is_company': True})
        cls.move = cls.env['account.move'].create({
            'move_type': 'in_invoice', 'expense_origin': 'card', 'partner_id': cls.merchant.id,
            'invoice_date': '2026-11-10',
            'invoice_line_ids': [Command.create({'name': 'Resto', 'quantity': 1, 'price_unit': 38.4})]})

    @classmethod
    def _user(cls, login, groups):
        return cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': login, 'login': login, 'email': f'{login}@example.com',
            'groups_id': [Command.set([g.id for g in groups])]})


@tagged('post_install', '-at_install')
class TestAttachPhotoToAnExistingExpense(CardExpenseCase):

    def _attachments(self):
        return self.env['ir.attachment'].search([('res_model', '=', 'account.move'), ('res_id', '=', self.move.id)])

    def test_the_holder_attaches_a_photo_and_gets_the_simplified_form(self):
        action = self.env['account.move'].with_user(self.holder).expense_attach_photo(self.move.id, _png_b64(), 'ticket.png')
        self.assertEqual(self._attachments().mapped('name'), ['ticket.png'])
        self.assertEqual((action['res_model'], action['res_id']), ('account.move', self.move.id))
        self.assertEqual(action['views'], [(self.env.ref('expense_core.view_expense_receipt_form').id, 'form')])

    def test_somebody_else_cannot_attach_a_photo(self):
        with self.assertRaises(AccessError):
            self.env['account.move'].with_user(self.other).expense_attach_photo(self.move.id, _png_b64())
        self.assertFalse(self._attachments())

    def test_a_photo_is_refused_on_something_that_is_not_an_expense(self):
        manual = self.env['account.move'].create({'move_type': 'in_invoice', 'partner_id': self.merchant.id})
        with self.assertRaises(UserError):
            self.env['account.move'].with_user(self.holder).expense_attach_photo(manual.id, _png_b64())

    def test_a_bad_image_is_refused(self):
        with self.assertRaises(UserError):
            self.env['account.move'].with_user(self.holder).expense_attach_photo(
                self.move.id, base64.b64encode(b'%PDF-1.4 pas une image').decode())

    def test_the_button_opens_the_capture_screen_for_this_expense(self):
        action = self.move.with_user(self.holder).action_expense_add_receipt_photo()
        self.assertEqual(action['tag'], 'expense_core.receipt_capture')
        self.assertEqual(action['context'], {'expense_move_id': self.move.id})

    def test_the_deep_link_opens_the_expense_on_the_simplified_form(self):
        self.assertEqual(self.move._expense_deep_link(),
                         '/odoo/action-expense_core.action_expense_open_receipt/%s' % self.move.id)
        action = self.env.ref('expense_core.action_expense_open_receipt')
        self.assertEqual(action.res_model, 'account.move')
        self.assertEqual(action.view_ids.view_id, self.env.ref('expense_core.view_expense_receipt_form'))


@tagged('post_install', '-at_install')
class TestPushNotificationLink(CardExpenseCase):
    """La notification mobile d'une dépense par carte porte l'adresse de l'écran simplifié (data.url)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env['ir.config_parameter'].sudo()
        params.set_param('mail.web_push_vapid_private_key', 'cle-privee-de-test')
        params.set_param('mail.web_push_vapid_public_key', 'cle-publique-de-test')
        cls.holder.notification_type = 'inbox'
        cls.env['mail.push.device'].sudo().create({
            'partner_id': cls.holder.partner_id.id, 'endpoint': 'https://push.example.com/endpoint-test',
            'keys': json.dumps({'p256dh': 'x', 'auth': 'y'})})

    def _sent_payload(self, **kwargs):
        with patch('odoo.addons.mail.models.mail_thread.push_to_end_point') as push:
            self.move._expense_notify(self.holder.partner_id, "Sujet", "Corps", **kwargs)
        self.assertEqual(push.call_count, 1)
        return json.loads(push.call_args[1]['payload'])

    def test_a_notification_with_a_link_carries_the_address_to_open(self):
        payload = self._sent_payload(deep_link=True)
        self.assertEqual(payload['options']['data']['url'], self.move._expense_deep_link())

    def test_a_notification_without_a_link_is_unchanged(self):
        payload = self._sent_payload()
        self.assertNotIn('url', payload['options']['data'])
        self.assertEqual(payload['options']['data']['model'], 'account.move')


@tagged('post_install', '-at_install')
class TestServiceWorker(HttpCase):

    def test_the_service_worker_opens_the_notification_address_in_a_new_tab(self):
        self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'SW test', 'login': 'sw_test', 'password': 'sw_test_pw', 'email': 'sw@example.com',
            'groups_id': [Command.set([self.env.ref('base.group_user').id])]})
        self.authenticate('sw_test', 'sw_test_pw')
        script = self.url_open('/web/service-worker.js').text
        self.assertIn('notificationclick', script)
        self.assertIn('self.clients.openWindow(url)', script)
        # notre traitement du clic passe avant celui d'Odoo, qu'il interrompt quand la notification porte une adresse
        self.assertLess(script.index('stopImmediatePropagation'), script.index('openDiscussChannel'))
