import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..controllers.main import QontoWebhook

BANK_ACCOUNT = 'acc-0001'


def _tx(**extra):
    tx = {
        'id': 'tx-1', 'transaction_id': 'tx-1', 'operation_type': 'card', 'side': 'debit', 'status': 'completed',
        'amount': 38.4, 'currency': 'EUR', 'label': 'Taxi Parisien', 'settled_at': '2026-11-10T09:00:00.000Z',
        'card_id': 'card-uuid-1', 'card_last_digits': '4242', 'bank_account_id': BANK_ACCOUNT,
    }
    tx.update(extra)
    return tx


@tagged('post_install', '-at_install')
class TestQontoExpense(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.journal = cls.env['account.journal'].create({
            'name': 'Qonto test', 'code': 'QNT', 'type': 'bank', 'statement_import_api_account_identifier': BANK_ACCOUNT})
        cls.employee = cls.env['hr.employee'].create({'name': 'Porteur test'})
        cls.default_partner = cls.env['res.partner'].create({'name': 'Dépenses carte', 'is_company': True})
        cls.company.qonto_default_partner_id = cls.default_partner
        cls.Move = cls.env['account.move']

    def _create(self, **extra):
        return self.Move._qonto_create_expense(self.company, _tx(**extra))

    def test_card_transaction_creates_a_draft_card_expense(self):
        move = self._create()
        self.assertEqual(move.expense_origin, 'card')
        self.assertEqual(move.state, 'draft')
        self.assertEqual(move.amount_total, 38.4)
        self.assertEqual(move.qonto_transaction_id, 'tx-1')
        self.assertEqual(str(move.invoice_date), '2026-11-10')
        self.assertEqual(move.partner_id, self.default_partner)

    def test_same_transaction_twice_creates_one_expense(self):
        first = self._create()
        self.assertEqual(self._create(), first)
        self.assertEqual(self.Move.search_count([('qonto_transaction_id', '=', 'tx-1')]), 1)

    def test_pending_declined_credit_and_non_card_transactions_are_ignored(self):
        for extra in ({'status': 'pending'}, {'status': 'declined'}, {'side': 'other'},
                      {'operation_type': 'transfer'}, {'card_id': None}):
            self.assertFalse(self._create(**extra), extra)

    def test_unknown_card_is_created_without_holder(self):
        move = self._create()
        card = move.expense_card_id
        self.assertEqual((card.code, card.qonto_card_id, card.journal_id), ('4242', 'card-uuid-1', self.journal))
        self.assertFalse(card.employee_id)
        self.assertIn("pas de porteur", move.message_ids[0].body)

    def test_known_card_gives_the_holder_as_spender_and_a_todo(self):
        user = self.env['res.users'].with_context(no_reset_password=True).create(
            {'name': 'Porteur', 'login': 'porteur_qonto', 'email': 'p@example.com'})
        self.employee.user_id = user
        self.env['account.bank.statement.card'].create({
            'code': '4242', 'qonto_card_id': 'card-uuid-1', 'journal_id': self.journal.id, 'employee_id': self.employee.id})
        move = self._create()
        self.assertEqual(move._get_expense_spender(), self.employee)
        self.assertTrue(move.activity_ids.filtered(lambda a: a.user_id == user))

    def test_merchant_fiche_with_the_same_name_is_the_partner(self):
        merchant = self.env['res.partner'].create({'name': 'Taxi Parisien', 'is_company': True})
        self.assertEqual(self._create().partner_id, merchant)

    def test_unknown_account_or_missing_partner_creates_nothing(self):
        self.assertFalse(self._create(bank_account_id='autre', card_id='card-x'))
        self.company.qonto_default_partner_id = False
        self.assertFalse(self._create(id='tx-2', transaction_id='tx-2'))

    def test_posting_registers_the_payment_on_the_card_journal(self):
        move = self._create()
        move.invoice_line_ids.tax_ids = False
        move.action_post()
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.payment_state, 'in_payment')
        payment = self.env['account.payment'].search([('journal_id', '=', self.journal.id)])
        self.assertEqual((len(payment), payment.amount, str(payment.date)), (1, 38.4, '2026-11-10'))

    def test_credit_transaction_creates_a_refund_paid_inbound(self):
        refund = self._create(id='tx-r', transaction_id='tx-r', side='credit', amount=12.5)
        self.assertEqual((refund.move_type, refund.amount_total), ('in_refund', 12.5))
        refund.invoice_line_ids.tax_ids = False
        refund.action_post()
        self.assertEqual(refund.state, 'posted')
        self.assertEqual(refund.payment_state, 'in_payment')
        self.assertFalse(refund.activity_ids)

    def test_holder_is_notified_by_the_odoo_bot(self):
        user = self.env['res.users'].with_context(no_reset_password=True).create(
            {'name': 'Porteur N', 'login': 'porteur_notif', 'email': 'pn@example.com'})
        self.employee.user_id = user
        self.env['account.bank.statement.card'].create({
            'code': '4242', 'qonto_card_id': 'card-uuid-1', 'journal_id': self.journal.id, 'employee_id': self.employee.id})
        move = self._create()
        message = self.env['mail.message'].search([('model', '=', 'account.move'), ('res_id', '=', move.id),
                                                   ('subject', 'ilike', 'à compléter')])
        self.assertEqual(message.partner_ids, user.partner_id)
        self.assertEqual(message.author_id, self.env.ref('base.partner_root'))

    def test_reminders_go_to_the_holder_then_to_the_management_and_stop(self):
        user = self.env['res.users'].with_context(no_reset_password=True).create(
            {'name': 'Porteur R', 'login': 'porteur_rem', 'email': 'pr@example.com'})
        boss = self.env['res.users'].with_context(no_reset_password=True).create(
            {'name': 'Direction R', 'login': 'boss_rem', 'email': 'br@example.com',
             'groups_id': [Command.set([self.env.ref('base.group_user').id, self.env.ref('taz-common.taz-management').id])]})
        self.employee.user_id = user
        self.env['account.bank.statement.card'].create({
            'code': '4242', 'qonto_card_id': 'card-uuid-1', 'journal_id': self.journal.id, 'employee_id': self.employee.id})
        move = self._create(settled_at='2026-01-01T09:00:00.000Z')
        subject = lambda s: self.env['mail.message'].search([('res_id', '=', move.id), ('subject', 'ilike', s)])
        self.assertEqual(self.Move._cron_qonto_reminders(), move)
        self.assertEqual(subject('Justificatif manquant').partner_ids, user.partner_id)
        self.assertFalse(self.Move._cron_qonto_reminders(), "pas de seconde relance avant le délai")
        move.qonto_reminder_date = '2026-01-10'
        self.assertEqual(self.Move._cron_qonto_reminders(), move)
        self.assertIn(boss.partner_id, subject('à relancer').partner_ids)
        move.qonto_reminder_date = '2026-01-20'
        self.assertFalse(self.Move._cron_qonto_reminders(), "au plus deux relances")

    def test_no_reminder_when_a_receipt_is_attached(self):
        move = self._create(settled_at='2026-01-01T09:00:00.000Z')
        self.env['ir.attachment'].create({'name': 'ticket.txt', 'res_model': 'account.move', 'res_id': move.id, 'raw': b'x'})
        self.assertFalse(self.Move._cron_qonto_reminders())

    def test_cron_reads_recent_card_transactions_once(self):
        with patch.object(type(self.env['qonto.api']), 'list_transactions',
                          return_value=[_tx(), _tx(id='tx-3', transaction_id='tx-3', status='pending')]):
            created = self.Move._cron_qonto_import()
            self.assertEqual(created.mapped('qonto_transaction_id'), ['tx-1'])
            self.assertTrue(self.journal.statement_import_api_last_success)
            self.assertFalse(self.Move._cron_qonto_import() - created)
        self.assertEqual(self.Move.search_count([('qonto_transaction_id', '=', 'tx-1')]), 1)


@tagged('post_install', '-at_install')
class TestQontoWebhook(HttpCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.qonto_webhook_secret = 'topsecret'
        self.env['account.journal'].create({
            'name': 'Qonto wh', 'code': 'QWH', 'type': 'bank', 'statement_import_api_account_identifier': BANK_ACCOUNT})
        self.company.qonto_default_partner_id = self.env['res.partner'].create({'name': 'Divers wh', 'is_company': True})

    def _post(self, payload, secret='topsecret', timestamp='1700000000'):
        body = json.dumps(payload).encode()
        signature = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        return self.url_open('/expense_qonto/webhook', data=body,
                             headers={'X-Qonto-Signature': 't=%s,v1=%s' % (timestamp, signature)})

    def test_signature_helper(self):
        body = b'{}'
        good = hmac.new(b's', b'1.' + body, hashlib.sha256).hexdigest()
        self.assertTrue(QontoWebhook._signature_matches('s', 't=1,v1=%s' % good, body))
        self.assertFalse(QontoWebhook._signature_matches('s', 't=1,v1=abc', body))
        self.assertFalse(QontoWebhook._signature_matches('s', None, body))
        self.assertFalse(QontoWebhook._signature_matches(False, 't=1,v1=%s' % good, body))

    def test_bad_signature_is_refused_and_creates_nothing(self):
        self.assertEqual(self._post({'data': _tx()}, secret='wrong').status_code, 401)
        self.assertFalse(self.env['account.move'].search([('qonto_transaction_id', '=', 'tx-1')]))

    def test_signed_call_creates_the_expense_once(self):
        self.assertEqual(self._post({'data': _tx()}).status_code, 200)
        self.assertEqual(self._post({'data': _tx()}).status_code, 200)
        self.assertEqual(self.env['account.move'].search_count([('qonto_transaction_id', '=', 'tx-1')]), 1)


@tagged('post_install', '-at_install')
class TestQontoWebhookRegistration(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.write({'statement_import_api_login': 'org-slug', 'statement_import_api_password': 'key',
                            'qonto_webhook_secret': False, 'qonto_webhook_subscription_id': False})
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://odoo.example.com')

    def _response(self, status, body=None):
        response = MagicMock(status_code=status, text='')
        response.content = b'{}' if body is not None else b''
        response.json.return_value = body or {}
        return response

    def test_register_sends_the_callback_and_keeps_our_secret_and_the_subscription(self):
        with patch('odoo.addons.expense_qonto.models.qonto_api.requests.request',
                   return_value=self._response(201, {'id': 'sub-1'})) as request:
            self.company.action_qonto_register_webhook()
        args, kwargs = request.call_args
        self.assertEqual(args[0], 'POST')
        self.assertTrue(args[1].endswith('/v2/webhook_subscriptions'))
        self.assertEqual(kwargs['json']['callback_url'], 'https://odoo.example.com/expense_qonto/webhook')
        self.assertEqual(kwargs['json']['types'], ['v1/transactions'])
        self.assertEqual(kwargs['headers']['Authorization'], 'org-slug:key')
        self.assertEqual(self.company.qonto_webhook_secret, kwargs['json']['secret'])
        self.assertTrue(32 <= len(kwargs['json']['secret']) <= 128)
        self.assertEqual(self.company.qonto_webhook_subscription_id, 'sub-1')

    def test_register_refuses_http_and_a_second_subscription(self):
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'http://odoo.example.com')
        with self.assertRaises(UserError):
            self.company.action_qonto_register_webhook()
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://odoo.example.com')
        self.company.qonto_webhook_subscription_id = 'sub-1'
        with self.assertRaises(UserError):
            self.company.action_qonto_register_webhook()

    def test_qonto_error_is_reported_and_nothing_is_kept(self):
        with patch('odoo.addons.expense_qonto.models.qonto_api.requests.request', return_value=self._response(401, {})):
            with self.assertRaises(UserError):
                self.company.action_qonto_register_webhook()
        self.assertFalse(self.company.qonto_webhook_subscription_id)
        self.assertFalse(self.company.qonto_webhook_secret)

    def test_unregister_deletes_the_subscription_and_clears_the_secret(self):
        self.company.write({'qonto_webhook_subscription_id': 'sub-1', 'qonto_webhook_secret': 'x' * 40})
        with patch('odoo.addons.expense_qonto.models.qonto_api.requests.request',
                   return_value=self._response(204)) as request:
            self.company.action_qonto_unregister_webhook()
        self.assertEqual(request.call_args[0][0], 'DELETE')
        self.assertTrue(request.call_args[0][1].endswith('/v2/webhook_subscriptions/sub-1'))
        self.assertFalse(self.company.qonto_webhook_subscription_id)
        self.assertFalse(self.company.qonto_webhook_secret)

    def test_settings_buttons_call_the_company_and_reload(self):
        settings = self.env['res.config.settings'].create({})
        with patch('odoo.addons.expense_qonto.models.qonto_api.requests.request',
                   return_value=self._response(201, {'id': 'sub-2'})):
            action = settings.action_qonto_register_webhook()
        self.assertEqual(action['tag'], 'reload')
        self.assertEqual(self.company.qonto_webhook_subscription_id, 'sub-2')

    def test_sandbox_url_and_staging_token_come_from_system_parameters(self):
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('expense_qonto.base_url', 'https://thirdparty-sandbox.staging.qonto.co/v2/')
        params.set_param('expense_qonto.staging_token', 'tok')
        with patch('odoo.addons.expense_qonto.models.qonto_api.requests.request',
                   return_value=self._response(201, {'id': 'sub-3'})) as request:
            self.company.action_qonto_register_webhook()
        self.assertEqual(request.call_args[0][1], 'https://thirdparty-sandbox.staging.qonto.co/v2/webhook_subscriptions')
        self.assertEqual(request.call_args[1]['headers']['X-Qonto-Staging-Token'], 'tok')
