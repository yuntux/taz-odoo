import base64

from lxml import etree

from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

QONTO_TEST_IBAN = 'FR7630006000011234567890189'
SUPPLIER_TEST_IBAN = 'FR1430004000011234567890125'


@tagged('post_install', '-at_install')
class TestPaymentFlow(TransactionCase):
    """Parcours UC1 de bout en bout : facture, bon à payer, comptabilisation, ordre de paiement, fichier SEPA."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        groups = cls.env.ref
        cls.office = cls._user('office_pay', [
            groups('base.group_user'), groups('account.group_account_invoice'), groups('taz-common.taz-adv')])
        cls.treasurer = cls._user('treasurer_pay', [
            groups('base.group_user'), groups('account.group_account_manager'),
            groups('account.group_validate_bank_account'), groups('base.group_partner_manager'),
            groups('account_payment_batch_oca.group_account_payment')])
        (groups('expense_core.tier_definition_bap_office') | groups('expense_core.tier_definition_bap_director')).write(
            {'active': True})

        # Journal Qonto fictif : l'IBAN réel viendra de l'ouverture du compte (lot 0, étape 0.1).
        company_bank = cls.env['res.partner.bank'].create({
            'acc_number': QONTO_TEST_IBAN,
            'partner_id': cls.company.partner_id.id,
            'bank_id': cls.env['res.bank'].create({'name': 'Qonto (test)', 'bic': 'QNTOFRP1XXX'}).id,
        })
        cls.journal = cls.env['account.journal'].create({
            'name': 'Qonto (test)', 'type': 'bank', 'code': 'QNTT',
            'bank_account_id': company_bank.id, 'company_id': cls.company.id,
        })
        method = cls.env.ref('account_payment_sepa_credit_transfer.sepa_credit_transfer')
        cls.method_line = cls.journal.outbound_payment_method_line_ids.filtered(
            lambda line: line.payment_method_id == method)[:1] or cls.env['account.payment.method.line'].create({
                'name': 'Virement SEPA fournisseurs', 'journal_id': cls.journal.id,
                'payment_method_id': method.id, 'selectable': True,
            })

        cls.company.expense_payment_method_line_id = cls.method_line

        cls.supplier = cls.env['res.partner'].create({'name': 'Fournisseur paiement test'})
        cls.supplier_bank = cls.env['res.partner.bank'].create({
            'acc_number': SUPPLIER_TEST_IBAN, 'partner_id': cls.supplier.id})

    @classmethod
    def _user(cls, login, groups):
        return cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': login, 'login': login, 'email': f'{login}@example.com',
            'groups_id': [Command.set([g.id for g in groups])],
        })

    def _bill(self, amount=120.0, ref='FA-TEST-1'):
        return self.env['account.move'].create({
            'move_type': 'in_invoice', 'partner_id': self.supplier.id, 'partner_bank_id': self.supplier_bank.id,
            'invoice_date': '2026-11-01', 'ref': ref,
            'invoice_line_ids': [Command.create({'name': 'Prestation', 'quantity': 1, 'price_unit': amount})],
        })

    def _approve(self, move):
        """Bon à payer d'une pièce sans projet : seule la validation de l'Office est nécessaire."""
        move.request_validation()
        self.env.invalidate_all()
        act = move.with_user(self.office).validate_tier()
        self.env['comment.wizard'].with_user(self.office).with_context(**act['context']).create(
            {'comment': 'Bon pour paiement'}).add_comment()
        self.env.invalidate_all()

    def test_unapproved_bill_cannot_reach_a_payment_order(self):
        bill = self._bill()
        with self.assertRaises(ValidationError):
            bill.action_post()
        with self.assertRaises(UserError):
            # Une pièce en brouillon n'entre pas dans un ordre de paiement.
            bill.create_account_payment_line()
        self.assertEqual(bill.state, 'draft')

    def test_company_default_payment_method_is_applied(self):
        bill = self._bill()
        self.assertEqual(bill.preferred_payment_method_line_id, self.method_line)
        self.supplier.property_outbound_payment_method_line_id = False
        # Une pièce sans mode préféré reçoit celui de la société au moment d'être payée.
        legacy = self._bill(10.0, 'FA-TEST-OLD')
        legacy.preferred_payment_method_line_id = False
        self._approve(legacy)
        legacy.action_post()
        legacy.with_user(self.treasurer).create_account_payment_line()
        self.assertEqual(legacy.preferred_payment_method_line_id, self.method_line)

    def test_untrusted_iban_blocks_the_order_until_the_bank_account_is_validated(self):
        bill = self._bill()
        self._approve(bill)
        bill.action_post()
        self.assertEqual(bill.state, 'posted')
        self.assertFalse(self.supplier_bank.allow_out_payment)

        action = bill.with_user(self.treasurer).create_account_payment_line()
        order = self.env['account.payment.order'].browse(action['res_id'])
        self.assertEqual(order.journal_id, self.journal)
        # Le compte du fournisseur n'est pas de confiance : l'ordre est refusé à la confirmation.
        with self.assertRaises(UserError):
            order.with_user(self.treasurer).draft2open()

        # Après vérification auprès du fournisseur, la trésorerie valide le compte.
        self.supplier_bank.with_user(self.treasurer).allow_out_payment = True
        order.with_user(self.treasurer).draft2open()
        self.assertEqual(order.state, 'open')

    def test_full_flow_generates_a_sepa_file(self):
        self.supplier_bank.with_user(self.treasurer).allow_out_payment = True
        bills = self._bill(120.0, 'FA-TEST-1') | self._bill(30.5, 'FA-TEST-2')
        for bill in bills:
            self._approve(bill)
            bill.action_post()
        for bill in bills:
            action = bill.with_user(self.treasurer).create_account_payment_line()
        order = self.env['account.payment.order'].browse(action['res_id'])
        self.assertEqual(len(order.payment_line_ids), 2)
        order = order.with_user(self.treasurer)
        order.draft2open()
        order.open2generated()
        self.assertEqual(order.state, 'generated')

        root = etree.fromstring(base64.b64decode(order.payment_file_id.datas))
        ns = {'p': root.nsmap[None]}
        xml = etree.tostring(root, pretty_print=True).decode()
        self.assertEqual(root.xpath('//p:PmtInf/p:PmtMtd', namespaces=ns)[0].text, 'TRF')
        self.assertEqual(root.xpath('//p:PmtInf/p:DbtrAcct/p:Id/p:IBAN', namespaces=ns)[0].text, QONTO_TEST_IBAN)
        creditor_ibans = root.xpath('//p:CdtTrfTxInf/p:CdtrAcct/p:Id/p:IBAN', namespaces=ns)
        self.assertTrue(creditor_ibans, xml)
        self.assertEqual(creditor_ibans[0].text, SUPPLIER_TEST_IBAN)
        total = sum(float(a.text) for a in root.xpath('//p:CdtTrfTxInf/p:Amt/p:InstdAmt', namespaces=ns))
        self.assertAlmostEqual(total, 150.5)

        order.generated2uploaded()
        self.assertEqual(order.state, 'uploaded')
        for bill in bills:
            self.assertIn(bill.payment_state, ('in_payment', 'paid'), (bill.payment_state, bill.amount_residual, order.payment_ids.mapped('state')))
