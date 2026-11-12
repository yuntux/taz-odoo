from odoo import fields, models


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    # Champ technique, jamais affiché : posé sur les lignes de BC créées à partir d'une dépense (note de frais, carte)
    # par le rapprochement automatique, pour les retrouver et les retirer si la dépense est annulée.
    expense_move_id = fields.Many2one(
        'account.move', string="Dépense à l'origine de la ligne", copy=False, index='btree_not_null',
        ondelete='set null')
