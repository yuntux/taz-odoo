import hashlib
import hmac
import json
from unittest.mock import patch

from odoo import Command
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
        for extra in ({'status': 'pending'}, {'status': 'declined'}, {'side': 'credit'},
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
