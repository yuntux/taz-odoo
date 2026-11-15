from odoo import fields, models


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    lucca_id = fields.Char(string="ID Lucca", readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Aucune notification pour les congés : ils sont gérés dans Lucca, puis rapatriés dans Odoo.
    #
    # Les messages restent écrits dans l'historique de la fiche (suivi des changements), mais personne n'est notifié, ni
    # par e-mail ni dans Odoo (notification mobile comprise). Odoo notifie par quatre chemins, tous coupés ici.
    # ------------------------------------------------------------------
    def _notify_thread(self, message, msg_vals=False, **kwargs):
        """Messages de la fiche (suivi, commentaires, activités attribuées) : écrits, jamais notifiés."""
        return []

    def _notify_manager(self):
        """Notification « Refused Time Off » envoyée au responsable des congés : supprimée."""
        return None

    def _force_cancel(self, reason, msg_subtype='mail.mt_comment', notify_responsibles=True):
        """Annulation : le message reste dans l'historique, le responsable n'est pas notifié."""
        return super()._force_cancel(reason, msg_subtype=msg_subtype, notify_responsibles=False)

    def _notify_change(self, message, subtype_xmlid='mail.mt_note'):
        """Changement de la demande : le message reste dans l'historique, l'employé n'est pas notifié."""
        for leave in self:
            leave.message_post(body=message, subtype_xmlid=subtype_xmlid)
