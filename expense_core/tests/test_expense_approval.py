from unittest.mock import patch

from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

from ..models.account_move import AccountMove


@tagged('post_install', '-at_install')
class TestExpenseApproval(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.office_def = cls.env.ref('expense_core.tier_definition_bap_office')
        cls.director_def = cls.env.ref('expense_core.tier_definition_bap_director')
        group_user = cls.env.ref('base.group_user')
        group_acc = cls.env.ref('account.group_account_invoice')
        cls.office = cls._user('office_test', [group_user, group_acc, cls.env.ref('taz-common.taz-adv')])
        cls.dm = cls._user('dm_test', [group_user, group_acc])
        cls.other = cls._user('other_test', [group_user, group_acc])
        cls.partner = cls.env['res.partner'].create({'name': 'Fournisseur BAP'})

    @classmethod
    def _user(cls, login, groups):
        return cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': login, 'login': login, 'email': f'{login}@example.com',
            'groups_id': [Command.set([g.id for g in groups])],
        })

    def _activate(self):
        (self.office_def | self.director_def).write({'active': True})

    def _bill(self, move_type='in_invoice'):
        return self.env['account.move'].create({
            'move_type': move_type,
            'partner_id': self.partner.id,
            'invoice_date': '2026-11-01',
            'invoice_line_ids': [Command.create({'name': 'Prestation', 'quantity': 1, 'price_unit': 100})],
        })

    def _directors(self, users):
        return patch.object(AccountMove, '_get_approval_directors', lambda rec: users)

    def _decide(self, move, user, action):
        """Valide ou refuse en passant par l'assistant de commentaire, comme dans l'interface."""
        act = getattr(move.with_user(user), action)()
        wizard = self.env['comment.wizard'].with_user(user).with_context(**act['context']).create(
            {'comment': 'commentaire'})
        wizard.add_comment()

    def test_tiers_are_shipped_inactive(self):
        self.assertFalse(self.office_def.active)
        self.assertFalse(self.director_def.active)
        move = self._bill()
        move.action_post()
        self.assertEqual(move.state, 'posted')

    def test_cannot_post_before_approval(self):
        self._activate()
        move = self._bill()
        with self.assertRaises(ValidationError):
            move.action_post()
        self.assertEqual(move.state, 'draft')

    def test_two_level_flow(self):
        self._activate()
        move = self._bill()
        with self._directors(self.dm):
            move.request_validation()
            self.env.invalidate_all()
            self.assertEqual(move.validation_status, 'pending')
            # Le niveau du DM attend celui de l'Office
            office_review = move.review_ids.filtered(lambda r: r.definition_id == self.office_def)
            dm_review = move.review_ids.filtered(lambda r: r.definition_id == self.director_def)
            self.assertEqual((office_review.status, dm_review.status), ('pending', 'waiting'))
            self.assertIn(self.office, office_review.reviewer_ids)
            self.assertEqual(dm_review.reviewer_ids, self.dm)
            self.assertNotIn(self.other, move.reviewer_ids)
            self._decide(move, self.office, 'validate_tier')
            self.assertEqual((office_review.status, dm_review.status), ('approved', 'pending'))
            self._decide(move, self.dm, 'validate_tier')
            self.assertEqual(move.validation_status, 'validated')
            self.env.invalidate_all()
            move.action_post()
        self.assertEqual(move.state, 'posted')

    def test_move_without_project_needs_only_office(self):
        self._activate()
        move = self._bill()
        with self._directors(self.env['res.users']):
            move.request_validation()
            self._decide(move, self.office, 'validate_tier')
        self.assertEqual(move.validation_status, 'validated')
        self.env.invalidate_all()
        move.action_post()
        self.assertEqual(move.state, 'posted')

    def test_rejection_by_director_blocks_posting(self):
        self._activate()
        move = self._bill()
        with self._directors(self.dm):
            move.request_validation()
            self._decide(move, self.office, 'validate_tier')
            self._decide(move, self.dm, 'reject_tier')
            self.assertEqual(move.validation_status, 'rejected')
            with self.assertRaises(ValidationError):
                move.action_post()

    def test_search_by_director(self):
        # La recherche doit fonctionner (rel_project_ids n'est pas stocké) et ne rien renvoyer pour un
        # utilisateur qui ne dirige aucun projet.
        found = self.env['account.move'].search([('approval_director_ids', 'in', self.other.ids)])
        self.assertFalse(found)

    def test_customer_invoice_is_not_concerned(self):
        self._activate()
        invoice = self._bill('out_invoice')
        self.assertFalse(invoice.need_validation)

    def test_review_filters_and_menu(self):
        """Les filtres « À valider par moi » et l'action de menu se chargent et se recherchent."""
        self._activate()
        move = self._bill()
        with self._directors(self.dm):
            move.request_validation()
        self.env.invalidate_all()
        mine = self.env['account.move'].with_user(self.office).search([('can_review', '=', True)])
        self.assertIn(move, mine)
        other = self.env['account.move'].with_user(self.other).search([('can_review', '=', True)])
        self.assertNotIn(move, other)
        action = self.env.ref('expense_core.action_expense_to_validate')
        self.assertEqual(action.res_model, 'account.move')
