from odoo import _, models
from odoo.exceptions import UserError


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def unlink(self):
        blocked = self.filtered(lambda l: l.move_id.sage_sync_status in ('sent', 'to_update'))
        if blocked:
            raise UserError(_(
                "Impossible de supprimer une ligne d'une pièce déjà envoyée à Sage : %(names)s. "
                "Il faut contre-passer la pièce plutôt que d'en modifier les lignes.",
                names=", ".join(blocked.mapped('move_id.name')),
            ))
        return super().unlink()
