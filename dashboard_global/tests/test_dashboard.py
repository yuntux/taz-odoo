from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestGlobalDashboard(TransactionCase):

    def test_cockpit_lists_expenses_to_validate(self):
        view = self.env.ref('dashboard_global.global_board_form')
        arch = self.env['board.board'].get_view(view.id)['arch']
        self.assertIn("Dépenses de mes projets à valider", arch)
        action = self.env.ref('expense_core.action_expense_to_validate')
        self.assertIn(str(action.id), arch)
        # Les listes d'origine du Cockpit restent en place
        self.assertIn("Mes projets en cours", arch)
