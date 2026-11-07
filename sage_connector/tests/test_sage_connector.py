from datetime import date
from unittest.mock import patch

from odoo.addons.project_accounting.models.res_partner import PROTECTED_FIELD_LIST
from odoo.tests.common import TransactionCase, tagged

from ..models import account_move as account_move_module
from ..models import res_partner as res_partner_module
from ..models.sage_client import SageConnectorError


@tagged('post_install', '-at_install')
class TestSageConnector(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        # Les paramètres API société sont volontairement laissés vides : aucun test ne doit
        # déclencher un vrai appel réseau (SDataClient() lève alors immédiatement UserError,
        # avalée par le connecteur -- rapide et déterministe). Chaque test qui a besoin d'un client
        # fonctionnel patche SDataClient directement.
        cls.journal = cls.env['account.journal'].search([
            ('type', '=', 'general'), ('company_id', '=', cls.company.id),
        ], limit=1)
        cls.journal.write({
            'sage_sync_date_start': date(2026, 12, 31),
            'sage_journal_code': 'VTE',
        })
        cls.partner = cls.env['res.partner'].create({'name': 'Client Sage Test'})
        cls.receivable_account = cls.env['account.account'].create({
            'code': 'SAGE41100', 'name': 'Clients (test)', 'account_type': 'asset_receivable',
        })
        cls.income_account = cls.env['account.account'].create({
            'code': 'SAGE70600', 'name': 'Prestations (test)', 'account_type': 'income',
        })

    def _create_move(self, move_date, with_lines=False, post=False):
        vals = {
            'move_type': 'entry',
            'journal_id': self.journal.id,
            'partner_id': self.partner.id,
            'date': move_date,
        }
        if with_lines:
            vals['line_ids'] = [
                (0, 0, {
                    'account_id': self.receivable_account.id, 'name': 'Ligne tiers',
                    'partner_id': self.partner.id, 'debit': 120.0, 'credit': 0.0,
                }),
                (0, 0, {
                    'account_id': self.income_account.id, 'name': 'Ligne produit',
                    'partner_id': False, 'debit': 0.0, 'credit': 120.0,
                }),
            ]
        move = self.env['account.move'].create(vals)
        if post:
            # Un essai d'envoi réel est tenté ici (SDataClient non simulé) : il échoue
            # silencieusement (pas de serveur réel sur ce faux endpoint), la pièce reste
            # 'to_send' -- exactement ce qu'on veut pour ensuite contrôler l'envoi via un client
            # simulé dans le test.
            move.action_post()
        return move

    # ------------------------------------------------------------------
    # Portée (§6.1 point 0)
    # ------------------------------------------------------------------

    def test_move_in_scope_after_journal_start_date(self):
        move = self._create_move(date(2027, 1, 15))
        self.assertTrue(move._sage_is_in_scope())

    def test_move_out_of_scope_before_journal_start_date(self):
        move = self._create_move(date(2020, 1, 15))
        self.assertFalse(move._sage_is_in_scope())

    def test_move_out_of_scope_without_journal_start_date(self):
        journal = self.env['account.journal'].search([
            ('type', '=', 'purchase'), ('company_id', '=', self.company.id),
        ], limit=1)
        move = self.env['account.move'].create({
            'move_type': 'entry', 'journal_id': journal.id, 'date': date(2027, 1, 15),
        })
        self.assertFalse(move._sage_is_in_scope())

    def test_move_out_of_scope_after_journal_end_date(self):
        self.journal.sage_sync_date_end = date(2027, 6, 1)
        move = self._create_move(date(2027, 7, 1))
        self.assertFalse(move._sage_is_in_scope())
        self.journal.sage_sync_date_end = False

    # ------------------------------------------------------------------
    # Mapping écriture (§7)
    # ------------------------------------------------------------------

    def test_journal_entry_lines_are_balanced_debit_credit(self):
        move = self._create_move(date(2027, 1, 15), with_lines=True)
        operations = move._sage_build_journal_entry_lines('session', 0)
        self.assertEqual(len(operations), 2)

        by_account = {op['compteGeneral']: op for op in operations}
        receivable = by_account[self.receivable_account.code]
        income = by_account[self.income_account.code]

        self.assertEqual(receivable['sens'], 'D')
        self.assertEqual(receivable['montant'], 120.0)
        self.assertEqual(income['sens'], 'C')
        self.assertEqual(income['montant'], 120.0)
        self.assertNotIn('compteAuxiliaire', income)
        for op in operations:
            self.assertEqual(op['numeroPiece'], move.name)
            self.assertEqual(op['SessionImport'], {'$uuid': 'session'})

    def test_journal_entry_lines_empty_without_lines(self):
        move = self._create_move(date(2027, 1, 15), with_lines=False)
        self.assertEqual(move._sage_build_journal_entry_lines('session', 0), [])

    # ------------------------------------------------------------------
    # Envoi groupé (§6.1 point 3)
    # ------------------------------------------------------------------

    def test_process_pending_batch_marks_matched_piece_as_sent(self):
        move = self._create_move(date(2027, 1, 15), with_lines=True, post=True)
        self.assertEqual(move.sage_sync_status, 'to_send')
        fake_result = {
            '$resources': [
                {'$httpMethod': 'EXEC', '$messages': ["Enregistrement pièce n° %s" % move.name]},
            ],
        }
        with patch.object(account_move_module, 'SDataClient') as mocked_client_cls:
            mocked_client_cls.return_value.batch.return_value = fake_result
            self.env['account.move']._sage_process_pending_batch()
        move.invalidate_recordset()
        self.assertEqual(move.sage_sync_status, 'sent')

    def test_process_pending_batch_keeps_rejected_piece_as_to_send(self):
        move = self._create_move(date(2027, 1, 15), with_lines=True, post=True)
        fake_result = {
            '$resources': [
                {'$httpMethod': 'EXEC', '$messages': [
                    "Erreur d'importation : lignes 1-:1",
                    "Le solde de la pièce %s doit être nul." % move.name,
                ]},
            ],
        }
        with patch.object(account_move_module, 'SDataClient') as mocked_client_cls:
            mocked_client_cls.return_value.batch.return_value = fake_result
            self.env['account.move']._sage_process_pending_batch()
        move.invalidate_recordset()
        self.assertEqual(move.sage_sync_status, 'to_send')

    def test_process_pending_batch_swallows_api_errors(self):
        move = self._create_move(date(2027, 1, 15), with_lines=True, post=True)
        with patch.object(account_move_module, 'SDataClient') as mocked_client_cls:
            mocked_client_cls.return_value.batch.side_effect = SageConnectorError("API indisponible")
            # Must not raise even though the API call fails.
            self.env['account.move']._sage_process_pending_batch()
        move.invalidate_recordset()
        self.assertEqual(move.sage_sync_status, 'to_send')

    def test_process_pending_batch_groups_several_moves_in_one_session(self):
        move1 = self._create_move(date(2027, 1, 15), with_lines=True, post=True)
        move2 = self._create_move(date(2027, 1, 16), with_lines=True, post=True)
        with patch.object(account_move_module, 'SDataClient') as mocked_client_cls:
            mocked_client_cls.return_value.batch.return_value = {'$resources': []}
            self.env['account.move']._sage_process_pending_batch()
        operations = mocked_client_cls.return_value.batch.call_args.args[0]
        session_ops = [op for op in operations if op.get('$url') == 'cptimport/tsessionimport']
        exec_ops = [op for op in operations if op.get('$httpMethod') == 'EXEC']
        line_ops = [op for op in operations if op.get('$url') == 'cptimport/timportecritures']
        self.assertEqual(len(session_ops), 1, "Un seul lot = une seule session partagée")
        self.assertEqual(len(exec_ops), 1)
        self.assertEqual(len(line_ops), 4, "2 lignes par move x 2 moves")

    # ------------------------------------------------------------------
    # Suppression (§6.2)
    # ------------------------------------------------------------------

    def test_unlink_blocked_once_sent(self):
        move = self._create_move(date(2027, 1, 15))
        move.sage_sync_status = 'sent'
        with self.assertRaises(Exception):
            move.unlink()

    def test_unlink_allowed_before_send(self):
        move = self._create_move(date(2027, 1, 15))
        move_id = move.id
        move.unlink()
        self.assertFalse(self.env['account.move'].browse(move_id).exists())

    def test_move_line_unlink_blocked_once_sent(self):
        move = self._create_move(date(2027, 1, 15), with_lines=True)
        move.sage_sync_status = 'sent'
        with self.assertRaises(Exception):
            move.line_ids[0].unlink()

    # ------------------------------------------------------------------
    # Protection des codes Sage sur un tiers déjà facturé
    # ------------------------------------------------------------------

    def test_sage_codes_are_in_protected_field_list(self):
        self.assertIn('sage_client_code', PROTECTED_FIELD_LIST)
        self.assertIn('sage_vendor_code', PROTECTED_FIELD_LIST)

    def test_sage_code_write_blocked_for_non_adv_on_protected_partner(self):
        company_partner = self.env['res.partner'].create({'name': 'Société Protégée', 'is_company': True})
        self._create_move(date(2027, 1, 1))  # un move référençant self.partner, pas celui-ci
        self.env['account.move'].create({
            'move_type': 'entry', 'journal_id': self.journal.id,
            'partner_id': company_partner.id, 'date': date(2027, 1, 1),
        })
        self.assertTrue(company_partner._precompute_protected())

        non_adv_user = self.env['res.users'].create({
            'name': 'Non ADV', 'login': 'non_adv_sage_test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(Exception):
            company_partner.with_user(non_adv_user).write({'sage_client_code': '411PROTEGE'})

    def test_search_cascade_auto_fills_code_from_siret_match(self):
        partner = self.env['res.partner'].create({'name': 'Tiers Sage Test', 'siret': '51327262500049'})
        fake_client = type('FakeClient', (), {
            'get_all_pages': lambda self, entity, params=None: (
                [{'Caption': '411TOTAL'}] if 'TClient' in entity else []
            ),
        })()
        with patch.object(res_partner_module, 'SDataClient', return_value=fake_client):
            partner._sage_search_cascade_auto()
        self.assertEqual(partner.sage_client_code, '411TOTAL')
        self.assertFalse(partner.sage_vendor_code)

    def test_write_never_pushes_to_sage(self):
        partner = self.env['res.partner'].create({'name': 'Tiers Sage Test'})
        partner.write({'sage_client_code': '411TOTAL'})
        with patch.object(res_partner_module, 'SDataClient') as mocked_client_cls:
            partner.write({'city': 'Lyon', 'name': 'Tiers Sage Test Renommé'})
        mocked_client_cls.assert_not_called()

    def test_create_role_rereads_actual_caption(self):
        partner = self.env['res.partner'].create({'name': 'Tiers Sage Test'})
        fake_client = type('FakeClient', (), {
            'batch': lambda self, operations: {},
            'get_all_pages': lambda self, entity, params=None: [{'Caption': '411TOTAL-REPREFIXE'}],
        })()
        with patch.object(res_partner_module, 'SDataClient', return_value=fake_client):
            code = partner._sage_create_role('client', code='411TOTAL')
        self.assertEqual(code, '411TOTAL-REPREFIXE')
        self.assertEqual(partner.sage_client_code, '411TOTAL-REPREFIXE')

    def test_action_sage_resolve_notifies_when_already_resolved(self):
        partner = self.env['res.partner'].create({'name': 'Tiers Sage Test'})
        partner.write({'sage_client_code': '411TOTAL', 'sage_vendor_code': '401TOTAL'})
        result = partner.action_sage_resolve()
        self.assertEqual(result.get('tag'), 'display_notification')

    def test_action_sage_resolve_creates_wizard_with_candidates(self):
        partner = self.env['res.partner'].create({'name': 'Tiers Sage Test', 'is_company': True})
        fake_client = type('FakeClient', (), {
            'get_all_pages': lambda self, entity, params=None: (
                [{'Caption': '411CANDIDATE', 'raisonSociale': 'Tiers Sage Test'}]
                if 'TClient' in entity else []
            ),
        })()
        with patch.object(res_partner_module, 'SDataClient', return_value=fake_client):
            action = partner.action_sage_resolve()
        self.assertEqual(action.get('res_model'), 'sage.partner.resolve.wizard')
        wizard = self.env['sage.partner.resolve.wizard'].search(action['domain'])
        self.assertTrue(wizard)
        client_wizard = wizard.filtered(lambda w: w.role == 'client')
        self.assertEqual(len(client_wizard.candidate_ids), 1)
        self.assertEqual(client_wizard.candidate_ids.code, '411CANDIDATE')
