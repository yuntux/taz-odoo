import json
from datetime import date
from unittest.mock import patch

from odoo.addons.project_accounting.models.res_partner import PROTECTED_FIELD_LIST
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..models import account_move as account_move_module
from ..models import res_partner as res_partner_module
from ..models.sage_client import SageConnectorError, SDataClient


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
        self.assertEqual(income['typeCompte'], 'G')
        self.assertEqual(receivable['typeCompte'], 'X')
        for op in operations:
            self.assertEqual(op['numeroPiece'], move.name)
            self.assertEqual(op['SessionImport'], {'$uuid': 'session'})

    def test_compte_auxiliaire_only_on_collectif_line(self):
        """Odoo propage `partner_id` sur toutes les lignes d'une facture (pas seulement la ligne
        client/fournisseur) -- `compteAuxiliaire` ne doit pourtant être envoyé que sur la ligne dont
        le compte est réellement "collectif" (recevable/payable), sinon Sage rejette l'import
        ("Le type de collectif Non du compte ... ne permet pas d'identifier le type du tiers").
        """
        self.partner.sage_client_code = '411TEST'
        sale_journal = self.env['account.journal'].search([
            ('type', '=', 'sale'), ('company_id', '=', self.company.id),
        ], limit=1)
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'journal_id': sale_journal.id,
            'partner_id': self.partner.id, 'date': date(2027, 1, 15),
        })
        move.line_ids = [
            (0, 0, {
                'account_id': self.receivable_account.id, 'name': 'Ligne tiers',
                'partner_id': self.partner.id, 'debit': 120.0, 'credit': 0.0,
            }),
            (0, 0, {
                'account_id': self.income_account.id, 'name': 'Ligne produit',
                'partner_id': self.partner.id, 'debit': 0.0, 'credit': 120.0,
            }),
        ]
        operations = move._sage_build_journal_entry_lines('session', 0)
        by_account = {op['compteGeneral']: op for op in operations}
        self.assertIn('compteAuxiliaire', by_account[self.receivable_account.code])
        self.assertEqual(by_account[self.receivable_account.code]['typeCompte'], 'X')
        self.assertNotIn('compteAuxiliaire', by_account[self.income_account.code])
        self.assertEqual(by_account[self.income_account.code]['typeCompte'], 'G')

    def test_journal_entry_lines_empty_without_lines(self):
        move = self._create_move(date(2027, 1, 15), with_lines=False)
        self.assertEqual(move._sage_build_journal_entry_lines('session', 0), [])

    def test_libelle_ecriture_truncated_to_sage_limit(self):
        long_name = 'X' * 200
        move = self._create_move(date(2027, 1, 15))
        move.line_ids = [
            (0, 0, {
                'account_id': self.receivable_account.id, 'name': long_name,
                'partner_id': self.partner.id, 'debit': 120.0, 'credit': 0.0,
            }),
            (0, 0, {
                'account_id': self.income_account.id, 'name': 'Ligne produit',
                'partner_id': False, 'debit': 0.0, 'credit': 120.0,
            }),
        ]
        operations = move._sage_build_journal_entry_lines('session', 0)
        receivable_op = next(op for op in operations if op['compteGeneral'] == self.receivable_account.code)
        self.assertEqual(len(receivable_op['libelleEcriture']), account_move_module.SAGE_LIBELLE_MAX_LENGTH)

    def test_document_type_fc_for_regular_client(self):
        sale_journal = self.env['account.journal'].search([
            ('type', '=', 'sale'), ('company_id', '=', self.company.id),
        ], limit=1)
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'journal_id': sale_journal.id,
            'partner_id': self.partner.id, 'date': date(2027, 1, 15),
        })
        self.assertEqual(move._sage_document_type(), 'FC')

    def test_document_type_fci_for_intragroup_client(self):
        # Volontairement pas de vrai res.company créé ici : ça déclenche une cascade de recalculs
        # (customer_book_goal/project_accounting) sans rapport, fragile sur cet environnement de
        # test -- on mocke directement le prédicat testé par ailleurs (recherche simple, peu de
        # risque) pour isoler `_sage_document_type()`.
        sale_journal = self.env['account.journal'].search([
            ('type', '=', 'sale'), ('company_id', '=', self.company.id),
        ], limit=1)
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'journal_id': sale_journal.id,
            'partner_id': self.partner.id, 'date': date(2027, 1, 15),
        })
        with patch.object(account_move_module.AccountMove, '_sage_is_intragroup_partner', return_value=True):
            self.assertEqual(move._sage_document_type(), 'FCI')

    def test_document_type_ff_for_regular_vendor(self):
        purchase_journal = self.env['account.journal'].search([
            ('type', '=', 'purchase'), ('company_id', '=', self.company.id),
        ], limit=1)
        move = self.env['account.move'].create({
            'move_type': 'in_invoice', 'journal_id': purchase_journal.id,
            'partner_id': self.partner.id, 'date': date(2027, 1, 15),
        })
        self.assertEqual(move._sage_document_type(), 'FF')

    def test_document_type_ffi_for_intragroup_vendor(self):
        purchase_journal = self.env['account.journal'].search([
            ('type', '=', 'purchase'), ('company_id', '=', self.company.id),
        ], limit=1)
        move = self.env['account.move'].create({
            'move_type': 'in_invoice', 'journal_id': purchase_journal.id,
            'partner_id': self.partner.id, 'date': date(2027, 1, 15),
        })
        with patch.object(account_move_module.AccountMove, '_sage_is_intragroup_partner', return_value=True):
            self.assertEqual(move._sage_document_type(), 'FFI')

    def test_document_type_af_for_vendor_refund_even_if_intragroup(self):
        purchase_journal = self.env['account.journal'].search([
            ('type', '=', 'purchase'), ('company_id', '=', self.company.id),
        ], limit=1)
        move = self.env['account.move'].create({
            'move_type': 'in_refund', 'journal_id': purchase_journal.id,
            'partner_id': self.partner.id, 'date': date(2027, 1, 15),
        })
        with patch.object(account_move_module.AccountMove, '_sage_is_intragroup_partner', return_value=True):
            self.assertEqual(move._sage_document_type(), 'AF')

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

    def test_search_by_name_uses_raison_sociale_for_company(self):
        partner = self.env['res.partner'].create({'name': 'Tiers Sage Test Raison Sociale', 'is_company': True})
        captured = {}

        def fake_get_all_pages(self, entity, params=None):
            captured['where'] = params['where']
            return []
        fake_client = type('FakeClient', (), {'get_all_pages': fake_get_all_pages})()
        partner._sage_search_by_name(fake_client, res_partner_module.SAGE_ENTITY_PARTNER_CLIENT)
        self.assertEqual(captured['where'], "tiers.raisonSociale eq 'Tiers Sage Test Raison Sociale'")

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
        self.assertEqual(action.get('res_model'), 'sage.partner.resolve.batch.wizard')
        batch = self.env['sage.partner.resolve.batch.wizard'].browse(action['res_id'])
        client_lines = batch.line_ids.filtered(lambda w: w.role == 'client')
        self.assertEqual(len(client_lines), 1)
        self.assertEqual(client_lines.code, '411CANDIDATE')
        self.assertFalse(client_lines.is_create_option)
        vendor_lines = batch.line_ids.filtered(lambda w: w.role == 'vendor')
        self.assertEqual(len(vendor_lines), 1)
        self.assertTrue(vendor_lines.is_create_option)
        self.assertTrue(vendor_lines.new_code)

    def test_action_sage_resolve_shows_resolved_role_as_a_grayed_line(self):
        partner = self.env['res.partner'].create({
            'name': 'Tiers Sage Test', 'sage_client_code': '411DEJA', 'is_company': True,
        })
        fake_client = type('FakeClient', (), {
            'get_all_pages': lambda self, entity, params=None: [],
        })()
        with patch.object(res_partner_module, 'SDataClient', return_value=fake_client):
            action = partner.action_sage_resolve()
        batch = self.env['sage.partner.resolve.batch.wizard'].browse(action['res_id'])
        client_lines = batch.line_ids.filtered(lambda w: w.role == 'client')
        self.assertTrue(client_lines.resolved)
        self.assertEqual(client_lines.code, '411DEJA')

    def test_action_sage_resolve_warns_without_overwriting_on_siret_mismatch(self):
        # Seul le rôle fournisseur manque au départ -- le rôle client, déjà résolu, est quand même
        # revérifié par la cascade : Sage renvoie un code différent, une alerte doit le signaler
        # sans jamais écraser '411DEJA'. Le rôle fournisseur, lui, se remplit via la cascade.
        partner = self.env['res.partner'].create({
            'name': 'Tiers Sage Test', 'siret': '51327262500049', 'sage_client_code': '411DEJA',
        })
        fake_client = type('FakeClient', (), {
            'get_all_pages': lambda self, entity, params=None: (
                [{'Caption': '411AUTRECODE'}] if 'TClient' in entity else [{'Caption': '401NOUVEAU'}]
            ),
        })()
        with patch.object(res_partner_module, 'SDataClient', return_value=fake_client):
            result = partner.action_sage_resolve()
        self.assertEqual(result['params']['type'], 'warning')
        self.assertEqual(partner.sage_client_code, '411DEJA')
        self.assertEqual(partner.sage_vendor_code, '401NOUVEAU')

    def test_batch_action_validate_attaches_selected_candidate_and_creates_other_role(self):
        partner = self.env['res.partner'].create({'name': 'Tiers Sage Test'})
        batch = self.env['sage.partner.resolve.batch.wizard'].create({
            'line_ids': [
                (0, 0, {
                    'partner_id': partner.id, 'role': 'client',
                    'code': '411EXISTANT', 'name': 'Tiers Sage Test', 'selected': True,
                }),
                (0, 0, {
                    'partner_id': partner.id, 'role': 'vendor', 'is_create_option': True,
                    'new_code': '401NOUVEAU', 'selected': True,
                }),
            ],
        })
        fake_client = type('FakeClient', (), {
            'batch': lambda self, operations: {},
            'get_all_pages': lambda self, entity, params=None: [{'Caption': '401NOUVEAU'}],
        })()
        with patch.object(res_partner_module, 'SDataClient', return_value=fake_client):
            result = batch.action_validate()
        self.assertEqual(partner.sage_client_code, '411EXISTANT')
        self.assertEqual(partner.sage_vendor_code, '401NOUVEAU')
        self.assertEqual(result.get('type'), 'ir.actions.act_window_close')

    def test_batch_action_validate_ignores_unselected_lines(self):
        partner = self.env['res.partner'].create({'name': 'Tiers Sage Test'})
        batch = self.env['sage.partner.resolve.batch.wizard'].create({
            'line_ids': [(0, 0, {
                'partner_id': partner.id, 'role': 'client',
                'code': '411CANDIDATE', 'name': 'Tiers Sage Test', 'selected': False,
            })],
        })
        batch.action_validate()
        self.assertFalse(partner.sage_client_code)

    def test_batch_action_validate_blocks_more_than_one_selected_per_role(self):
        partner = self.env['res.partner'].create({'name': 'Tiers Sage Test'})
        batch = self.env['sage.partner.resolve.batch.wizard'].create({
            'line_ids': [
                (0, 0, {
                    'partner_id': partner.id, 'role': 'client',
                    'code': '411A', 'name': 'A', 'selected': True,
                }),
                (0, 0, {
                    'partner_id': partner.id, 'role': 'client',
                    'code': '411B', 'name': 'B', 'selected': True,
                }),
            ],
        })
        with self.assertRaises(UserError):
            batch.action_validate()
        self.assertFalse(partner.sage_client_code)

    def test_batch_action_validate_blocked_if_role_already_resolved(self):
        partner = self.env['res.partner'].create({'name': 'Tiers Sage Test', 'sage_client_code': '411DEJA'})
        batch = self.env['sage.partner.resolve.batch.wizard'].create({
            'line_ids': [(0, 0, {
                'partner_id': partner.id, 'role': 'client',
                'code': '411AUTRE', 'name': 'Tiers Sage Test', 'selected': True,
            })],
        })
        with self.assertRaises(UserError):
            batch.action_validate()
        self.assertEqual(partner.sage_client_code, '411DEJA')

    def test_batch_action_validate_requires_a_new_code_for_create_option(self):
        partner = self.env['res.partner'].create({'name': 'Tiers Sage Test'})
        batch = self.env['sage.partner.resolve.batch.wizard'].create({
            'line_ids': [(0, 0, {
                'partner_id': partner.id, 'role': 'vendor', 'is_create_option': True, 'selected': True,
            })],
        })
        with patch.object(res_partner_module, 'SDataClient') as mocked_client_cls:
            with self.assertRaises(UserError):
                batch.action_validate()
        mocked_client_cls.assert_not_called()

    def test_request_encodes_spaces_as_percent20_not_plus(self):
        self.company.write({
            'sage_api_endpoint': 'https://sage.example.com',
            'sage_api_token': 'fake-token',
            'sage_contract_code': 'MC2I',
        })
        client = SDataClient(self.env)
        fake_response = type('FakeResponse', (), {
            'ok': True, 'status_code': 200, 'content': b'{}', 'json': lambda self: {},
        })()
        with patch.object(type(client.session), 'request', return_value=fake_response) as mocked_request:
            client.get('gentiers/TClient', params={'where': "tiers.codeSIRET eq '123'"})
        called_url = mocked_request.call_args.args[1]
        self.assertIn('%20', called_url)
        self.assertNotIn('+', called_url)

    def test_batch_error_message_surfaces_diagnosis_not_truncated_text(self):
        self.company.write({
            'sage_api_endpoint': 'https://sage.example.com',
            'sage_api_token': 'fake-token',
            'sage_contract_code': 'MC2I',
        })
        client = SDataClient(self.env)
        # Reproduit le cas observé en prod : session + plusieurs lignes en succès (201), suivies
        # d'une ligne en échec dont le diagnostic est niché sous sa propre clé "$" (pas directement
        # sous "$diagnoses", ni de "$httpStatus" du tout sur cette ressource) -- le tout dépassant
        # largement 500 caractères avant d'atteindre cette ligne en échec.
        padding = [{'$httpStatus': 201, '$httpMethod': 'POST', '$etag': 'X' * 60}] * 10
        failing_resource = {
            '$milliseconds': '6.6990',
            '$': {'$diagnoses': [{'severity': 'error', 'message': 'Le solde de la pièce doit être nul.'}]},
        }
        body = {'$resources': padding + [failing_resource]}
        fake_response = type('FakeResponse', (), {
            'ok': False, 'status_code': 400,
            'text': json.dumps(body),
        })()
        with patch.object(type(client.session), 'request', return_value=fake_response):
            with self.assertRaises(SageConnectorError) as ctx:
                client.batch([{'$httpMethod': 'POST'}])
        self.assertIn('Le solde de la pièce doit être nul.', str(ctx.exception))

    def test_request_logs_a_copy_pasteable_curl_command_with_token_masked(self):
        self.company.write({
            'sage_api_endpoint': 'https://sage.example.com',
            'sage_api_token': 'SUPER-SECRET-TOKEN',
            'sage_contract_code': 'MC2I',
        })
        client = SDataClient(self.env)
        command = client._as_curl_command(
            'POST', 'https://sage.example.com/compta/server/sdata/MC2I/$batch', {'a': 1},
        )
        self.assertTrue(command.startswith("curl -X POST"))
        self.assertIn("-d '{\"a\": 1}'", command)
        self.assertNotIn('SUPER-SECRET-TOKEN', command)
        self.assertIn('SAGE_API_TOKEN', command)
