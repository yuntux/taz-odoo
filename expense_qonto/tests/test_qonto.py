import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from odoo import Command, fields
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
        # Ces tests comptabilisent des pièces sans passer par le bon à payer (livré actif), qui a ses propres tests.
        (cls.env.ref('expense_core.tier_definition_bap_office')
         | cls.env.ref('expense_core.tier_definition_bap_director')).write({'active': False})
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

    def _ticket(self, **extra):
        return dict({'merchant': 'RESTO TICKET TEST', 'siret': '73282932000074', 'siren': '732829320',
                     'vat': 'FR44732829320', 'date': None, 'total': None}, **extra)

    def test_the_ticket_replaces_the_fallback_partner_with_the_merchant_found_by_siret(self):
        """Même code que les notes de frais : le tiers de repli laisse la place au commerçant lu sur le ticket."""
        merchant = self.env['res.partner'].create({'name': 'Les vrais Restos SAS', 'is_company': True,
                                                   'siret': '73282932000074'})
        move = self._create()
        self.assertEqual(move.partner_id, self.default_partner)
        move._apply_ocr_result(self._ticket())
        self.assertEqual(move.partner_id, merchant)
        self.assertEqual(move.expense_merchant_id, merchant)
        self.assertIn('Le tiers de la dépense est maintenant le commerçant', move.message_ids[0].body)

    def test_the_ticket_creates_the_merchant_when_no_fiche_knows_it(self):
        move = self._create()
        move._apply_ocr_result(self._ticket())
        self.assertEqual(move.partner_id.name, 'RESTO TICKET TEST')
        self.assertEqual(move.partner_id.siret, '73282932000074')
        self.assertNotEqual(move.partner_id, self.default_partner)

    def test_the_ticket_leaves_a_real_partner_alone(self):
        real = self.env['res.partner'].create({'name': 'Carte Resto', 'is_company': True})
        other = self.env['res.partner'].create({'name': 'Autre', 'is_company': True, 'siret': '73282932000074'})
        move = self._create(label='Carte Resto')
        self.assertEqual(move.partner_id, real)
        move._apply_ocr_result(self._ticket())
        self.assertEqual(move.partner_id, real)
        self.assertEqual(move.expense_merchant_id, other)

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
                            'qonto_auth_mode': 'oauth', 'qonto_access_token': 'tok-acces',
                            'qonto_refresh_token': 'tok-renouv',
                            'qonto_access_token_expiry': fields.Datetime.now() + timedelta(hours=1),
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
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer tok-acces')
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

    def test_production_sends_no_staging_token(self):
        self.company.write({'qonto_environment': 'production'})
        with patch('odoo.addons.expense_qonto.models.qonto_api.requests.request',
                   return_value=self._response(201, {'id': 'sub-3'})) as request:
            self.company.action_qonto_register_webhook()
        self.assertEqual(request.call_args[0][1], 'https://thirdparty.qonto.com/v2/webhook_subscriptions')
        self.assertNotIn('X-Qonto-Staging-Token', request.call_args[1]['headers'])

    def test_sandbox_uses_the_sandbox_url_and_the_staging_token_of_the_company(self):
        self.company.write({'qonto_environment': 'sandbox', 'qonto_staging_token': 'tok'})
        with patch('odoo.addons.expense_qonto.models.qonto_api.requests.request',
                   return_value=self._response(201, {'id': 'sub-3'})) as request:
            self.company.action_qonto_register_webhook()
        self.assertEqual(request.call_args[0][1], 'https://thirdparty-sandbox.staging.qonto.co/v2/webhook_subscriptions')
        self.assertEqual(request.call_args[1]['headers']['X-Qonto-Staging-Token'], 'tok')

    def test_sandbox_without_a_staging_token_refuses_to_call_qonto(self):
        self.company.write({'qonto_environment': 'sandbox', 'qonto_staging_token': False})
        with patch('odoo.addons.expense_qonto.models.qonto_api.requests.request') as request:
            with self.assertRaises(UserError):
                self.company.action_qonto_register_webhook()
        request.assert_not_called()

    def test_the_settings_screen_saves_the_environment_and_the_token_together(self):
        """L'écran écrit l'environnement puis le jeton sur la société : l'enregistrement ne doit pas refuser le premier
        avant que le second ne soit écrit."""
        settings = self.env['res.config.settings'].create({
            'qonto_environment': 'sandbox', 'qonto_staging_token': 'tok-ecran'})
        settings.execute()
        self.assertEqual(self.company.qonto_environment, 'sandbox')
        self.assertEqual(self.company.qonto_staging_token, 'tok-ecran')

    def test_the_webhook_secret_is_read_only_in_the_settings(self):
        self.assertTrue(self.env['res.config.settings']._fields['qonto_webhook_secret'].readonly)


@tagged('post_install', '-at_install')
class TestQontoOauth(TransactionCase):
    """Connexion OAuth 2.0 : adresse d'autorisation, échange du code, renouvellement du jeton, mode clé d'API."""

    REQUESTS = 'odoo.addons.expense_qonto.models.qonto_api.requests'

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.write({
            'qonto_auth_mode': 'oauth', 'qonto_environment': 'production', 'qonto_staging_token': False,
            'qonto_oauth_client_id': 'client-1', 'qonto_oauth_client_secret': 'secret-1',
            'qonto_access_token': False, 'qonto_refresh_token': False, 'qonto_access_token_expiry': False,
            'qonto_oauth_connected_on': False, 'qonto_oauth_state': False,
            'qonto_webhook_secret': False, 'qonto_webhook_subscription_id': False})
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://odoo.example.com')

    def _tokens(self, access='acces-1', refresh='renouv-1', expires_in=3600):
        response = MagicMock(status_code=200, text='')
        response.json.return_value = {'access_token': access, 'refresh_token': refresh, 'expires_in': expires_in}
        return response

    def test_oauth_is_the_default_with_webhook_transactions_and_cards(self):
        company = self.env['res.company'].create({'name': 'Société sans réglage Qonto'})
        self.assertEqual(company.qonto_auth_mode, 'oauth')
        self.assertEqual(company.qonto_environment, 'production')
        self.assertEqual(set(company.qonto_oauth_scope_ids.mapped('code')),
                         {'offline_access', 'webhook', 'organization.read', 'card.read'})

    def test_connect_sends_the_administrator_to_the_authorization_page_of_qonto(self):
        action = self.company.action_qonto_oauth_connect()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        url = urlparse(action['url'])
        self.assertEqual(url.geturl().split('?')[0], 'https://oauth.qonto.com/oauth2/auth')
        query = {key: value[0] for key, value in parse_qs(url.query).items()}
        self.assertEqual(query['client_id'], 'client-1')
        self.assertEqual(query['response_type'], 'code')
        self.assertEqual(query['redirect_uri'], 'https://odoo.example.com/expense_qonto/oauth/callback')
        self.assertEqual(set(query['scope'].split()), {'offline_access', 'webhook', 'organization.read', 'card.read'})
        self.assertEqual(query['state'], self.company.qonto_oauth_state)
        self.assertTrue(query['state'].startswith('%s-' % self.company.id))

    def test_offline_access_is_always_requested(self):
        self.company.qonto_oauth_scope_ids = self.env['qonto.oauth.scope'].search([('code', '=', 'webhook')])
        self.assertIn('offline_access', self.company._qonto_oauth_scope_codes())

    def test_the_sandbox_uses_the_sandbox_authorization_page(self):
        self.company.write({'qonto_environment': 'sandbox', 'qonto_staging_token': 'tok'})
        action = self.company.action_qonto_oauth_connect()
        self.assertTrue(action['url'].startswith('https://oauth-sandbox.staging.qonto.co/oauth2/auth?'))

    def test_connect_needs_the_client_credentials_and_https(self):
        self.company.qonto_oauth_client_secret = False
        with self.assertRaises(UserError):
            self.company.action_qonto_oauth_connect()
        self.company.qonto_oauth_client_secret = 'secret-1'
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'http://odoo.example.com')
        with self.assertRaises(UserError):
            self.company.action_qonto_oauth_connect()

    def test_the_callback_exchanges_the_code_for_the_tokens(self):
        self.company.action_qonto_oauth_connect()
        with patch(self.REQUESTS + '.post', return_value=self._tokens()) as post:
            self.company._qonto_oauth_finish('le-code', self.company.qonto_oauth_state)
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://oauth.qonto.com/oauth2/token')
        self.assertEqual(kwargs['data'], {
            'grant_type': 'authorization_code', 'code': 'le-code', 'client_id': 'client-1',
            'client_secret': 'secret-1', 'redirect_uri': 'https://odoo.example.com/expense_qonto/oauth/callback'})
        self.assertNotIn('Authorization', kwargs['headers'])  # les identifiants vont dans le corps, jamais en Basic
        self.assertEqual((self.company.qonto_access_token, self.company.qonto_refresh_token), ('acces-1', 'renouv-1'))
        self.assertTrue(self.company.qonto_oauth_connected_on)
        self.assertFalse(self.company.qonto_oauth_state)  # l'état est à usage unique

    def test_a_wrong_state_is_refused_and_nothing_is_exchanged(self):
        self.company.action_qonto_oauth_connect()
        with patch(self.REQUESTS + '.post') as post:
            with self.assertRaises(UserError):
                self.company._qonto_oauth_finish('le-code', 'un-autre-etat')
            with self.assertRaises(UserError):
                self.company._qonto_oauth_finish('le-code', False)
        post.assert_not_called()
        self.assertFalse(self.company.qonto_access_token)

    def test_an_expired_access_token_is_refreshed_and_the_new_refresh_token_is_kept(self):
        self.company.write({
            'qonto_access_token': 'vieux', 'qonto_refresh_token': 'renouv-1',
            'qonto_access_token_expiry': fields.Datetime.now() - timedelta(minutes=1)})
        api_response = MagicMock(status_code=200, text='')
        api_response.content = b'{"transactions": []}'
        api_response.json.return_value = {'transactions': []}
        with patch(self.REQUESTS + '.post', return_value=self._tokens('acces-2', 'renouv-2')) as post, \
                patch(self.REQUESTS + '.request', return_value=api_response) as request:
            self.env['qonto.api']._get(self.company, 'transactions')
        self.assertEqual(post.call_args[1]['data']['grant_type'], 'refresh_token')
        self.assertEqual(post.call_args[1]['data']['refresh_token'], 'renouv-1')
        self.assertEqual(request.call_args[1]['headers']['Authorization'], 'Bearer acces-2')
        self.assertEqual(self.company.qonto_refresh_token, 'renouv-2')

    def test_a_valid_access_token_is_not_refreshed(self):
        self.company.write({
            'qonto_access_token': 'valide', 'qonto_refresh_token': 'renouv-1',
            'qonto_access_token_expiry': fields.Datetime.now() + timedelta(minutes=30)})
        api_response = MagicMock(status_code=200, text='')
        api_response.content = b'{}'
        api_response.json.return_value = {}
        with patch(self.REQUESTS + '.post') as post, patch(self.REQUESTS + '.request', return_value=api_response) as request:
            self.env['qonto.api']._get(self.company, 'transactions')
        post.assert_not_called()
        self.assertEqual(request.call_args[1]['headers']['Authorization'], 'Bearer valide')

    def test_oauth_without_a_connection_asks_to_connect(self):
        with patch(self.REQUESTS + '.request') as request:
            with self.assertRaises(UserError):
                self.env['qonto.api']._get(self.company, 'transactions')
        request.assert_not_called()

    def test_the_sandbox_sends_the_staging_token_to_the_token_endpoint_too(self):
        self.company.write({'qonto_environment': 'sandbox', 'qonto_staging_token': 'tok-staging'})
        self.company.action_qonto_oauth_connect()
        with patch(self.REQUESTS + '.post', return_value=self._tokens()) as post:
            self.company._qonto_oauth_finish('le-code', self.company.qonto_oauth_state)
        self.assertEqual(post.call_args[0][0], 'https://oauth-sandbox.staging.qonto.co/oauth2/token')
        self.assertEqual(post.call_args[1]['headers']['X-Qonto-Staging-Token'], 'tok-staging')

    def test_api_key_mode_still_calls_qonto_with_the_key_and_cannot_register_the_webhook(self):
        self.company.write({'qonto_auth_mode': 'api_key', 'statement_import_api_login': 'org-slug',
                            'statement_import_api_password': 'key'})
        response = MagicMock(status_code=200, text='')
        response.content = b'{}'
        response.json.return_value = {}
        with patch(self.REQUESTS + '.request', return_value=response) as request:
            self.env['qonto.api']._get(self.company, 'transactions')
        self.assertEqual(request.call_args[1]['headers']['Authorization'], 'org-slug:key')
        with self.assertRaises(UserError):
            self.company.action_qonto_register_webhook()

    def test_disconnect_forgets_the_tokens(self):
        self.company.write({'qonto_access_token': 'a', 'qonto_refresh_token': 'r',
                            'qonto_oauth_connected_on': fields.Datetime.now()})
        self.company.action_qonto_oauth_disconnect()
        self.assertFalse(self.company.qonto_access_token or self.company.qonto_refresh_token
                         or self.company.qonto_oauth_connected_on)

    def test_the_settings_screen_has_the_oauth_fields(self):
        settings = self.env['res.config.settings'].create({'qonto_oauth_client_id': 'c-ecran'})
        settings.execute()
        self.assertEqual(self.company.qonto_oauth_client_id, 'c-ecran')
        self.assertTrue(settings.qonto_oauth_redirect_uri.endswith('/expense_qonto/oauth/callback'))

    def test_a_registered_redirect_address_replaces_the_proposed_one_everywhere(self):
        registered = 'https://autre-domaine.example.com/qonto/retour'
        self.company.qonto_oauth_redirect_custom = registered
        self.assertEqual(self.company.qonto_oauth_redirect_uri, registered)
        query = parse_qs(urlparse(self.company.action_qonto_oauth_connect()['url']).query)
        self.assertEqual(query['redirect_uri'], [registered])
        with patch(self.REQUESTS + '.post', return_value=self._tokens()) as post:
            self.company._qonto_oauth_finish('le-code', self.company.qonto_oauth_state)
        self.assertEqual(post.call_args[1]['data']['redirect_uri'], registered)

    def test_the_pasted_return_address_finishes_the_connection(self):
        self.company.action_qonto_oauth_connect()
        state = self.company.qonto_oauth_state
        with patch(self.REQUESTS + '.post', return_value=self._tokens()) as post:
            self.company.action_qonto_oauth_finish_pasted(
                'http://localhost:3000/callback?code=le-code&state=%s' % state)
        self.assertEqual(post.call_args[1]['data']['code'], 'le-code')
        self.assertEqual(self.company.qonto_refresh_token, 'renouv-1')

    def test_the_pasted_code_alone_or_code_and_state_work_when_a_connection_is_pending(self):
        for pasted in ('le-code', 'code=le-code'):
            self.company.action_qonto_oauth_connect()
            with patch(self.REQUESTS + '.post', return_value=self._tokens()) as post:
                self.company.action_qonto_oauth_finish_pasted(pasted)
            self.assertEqual(post.call_args[1]['data']['code'], 'le-code', pasted)

    def test_a_pasted_address_with_another_state_or_without_a_pending_connection_is_refused(self):
        with patch(self.REQUESTS + '.post') as post:
            with self.assertRaises(UserError):  # aucune connexion en cours
                self.company.action_qonto_oauth_finish_pasted('le-code')
            self.company.action_qonto_oauth_connect()
            with self.assertRaises(UserError):  # état d'une autre connexion
                self.company.action_qonto_oauth_finish_pasted('https://x.example.com/?code=c&state=autre')
            with self.assertRaises(UserError):  # rien de lisible
                self.company.action_qonto_oauth_finish_pasted('   ')
            with self.assertRaises(UserError):  # une adresse sans code
                self.company.action_qonto_oauth_finish_pasted('https://x.example.com/?error=access_denied')
        post.assert_not_called()

    def test_the_settings_button_finishes_with_the_pasted_address(self):
        self.company.action_qonto_oauth_connect()
        settings = self.env['res.config.settings'].create({'qonto_oauth_return_url': 'le-code'})
        with patch(self.REQUESTS + '.post', return_value=self._tokens()):
            action = settings.action_qonto_oauth_finish_pasted()
        self.assertEqual(action['tag'], 'reload')
        self.assertEqual(self.company.qonto_access_token, 'acces-1')
