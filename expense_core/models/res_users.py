from odoo import Command, api, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    @api.model_create_multi
    def create(self, vals_list):
        """Les notifications des dépenses (alertes, relances, carte à compléter) doivent arriver en push : un nouvel
        utilisateur interne est donc créé avec la préférence « Gérer dans Odoo » (boîte de réception, qui déclenche le
        push de la PWA). Il peut la changer dans ses préférences ; un compte portail ou public reste en e-mail, comme
        le standard l'impose."""
        external = {self.env.ref('base.group_portal').id, self.env.ref('base.group_public').id}
        for vals in vals_list:
            if 'notification_type' in vals or vals.get('share'):
                continue
            ids = set()
            for command in vals.get('groups_id') or []:
                if command[0] == Command.LINK:
                    ids.add(command[1])
                elif command[0] == Command.SET:
                    ids |= set(command[2])
            if not ids & external:
                vals['notification_type'] = 'inbox'
        return super().create(vals_list)
