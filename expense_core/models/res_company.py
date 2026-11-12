from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    expense_payment_method_line_id = fields.Many2one(
        'account.payment.method.line', string="Mode de paiement des dépenses",
        domain="[('payment_type', '=', 'outbound'), ('payment_order_ok', '=', True)]",
        check_company=True,
        help="Mode de paiement (journal et méthode, par exemple « Virement SEPA fournisseurs » sur le journal "
             "Qonto) appliqué aux factures et reçus fournisseurs dont le fournisseur n'a pas de mode préféré.",
    )

    expense_reimbursement_method_line_id = fields.Many2one(
        'account.payment.method.line', string="Mode de paiement des remboursements",
        domain="[('payment_type', '=', 'outbound'), ('payment_order_ok', '=', True)]",
        check_company=True,
        help="Mode de paiement (journal et méthode, par exemple « Virement SEPA remboursements » sur le journal "
             "Qonto) appliqué aux reçus de salariés à rembourser. La tâche hebdomadaire les ajoute à un ordre "
             "de paiement en brouillon que la trésorerie lance.",
    )
