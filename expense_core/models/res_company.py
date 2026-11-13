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

    expense_alert_amount = fields.Monetary(
        string="Seuil d'alerte par dépense", default=500.0, currency_field='currency_id',
        help="Une dépense envoyée en validation au-dessus de ce montant est signalée par e-mail au directeur de "
             "mission, ou à la direction si la pièce n'a pas de projet.")
    expense_cumul_amount = fields.Monetary(
        string="Seuil de cumul par personne", default=1500.0, currency_field='currency_id',
        help="Alerte quand le cumul des dépenses d'une personne dépasse ce montant sur la durée ci-dessous ; "
             "le message va à son manager, ou à la direction.")
    expense_cumul_days = fields.Integer(
        string="Durée du cumul (jours)", default=30, help="Fenêtre glissante du cumul par personne.")
