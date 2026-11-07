from odoo import _, fields, models
from odoo.exceptions import UserError


class SagePartnerResolveWizard(models.TransientModel):
    """Un rôle manquant (client ou fournisseur) à résoudre pour un tiers, avec les candidats
    trouvés par recherche nominative (§8.1 critère 4) -- jamais de rattachement automatique sur
    un simple match de nom, toujours une confirmation ici (§8.0, §8.2).
    """
    _name = 'sage.partner.resolve.wizard'
    _description = "Résolution d'un tiers sur Sage"

    partner_id = fields.Many2one('res.partner', required=True, readonly=True)
    role = fields.Selection([
        ('client', 'Client'),
        ('vendor', 'Fournisseur'),
    ], required=True, readonly=True)
    candidate_ids = fields.One2many(
        'sage.partner.resolve.wizard.candidate', 'wizard_id', readonly=True,
        string="Tiers trouvés sur Sage par recherche nominative",
    )
    selected_candidate_id = fields.Many2one(
        'sage.partner.resolve.wizard.candidate', string="Rattacher ce tiers existant",
        domain="[('wizard_id', '=', id)]",
    )
    new_code = fields.Char(string="Ou créer un nouveau tiers avec ce code")

    def action_link_selected(self):
        self.ensure_one()
        if not self.selected_candidate_id:
            raise UserError(_("Sélectionnez un tiers existant dans la liste, ou saisissez un code pour en créer un nouveau."))
        field_name = 'sage_client_code' if self.role == 'client' else 'sage_vendor_code'
        self.partner_id.write({field_name: self.selected_candidate_id.code})

    def action_create_new(self):
        self.ensure_one()
        self.partner_id._sage_create_role(self.role, code=self.new_code or None)


class SagePartnerResolveWizardCandidate(models.TransientModel):
    _name = 'sage.partner.resolve.wizard.candidate'
    _description = "Candidat tiers Sage trouvé par recherche nominative"

    wizard_id = fields.Many2one('sage.partner.resolve.wizard', required=True, ondelete='cascade')
    code = fields.Char(readonly=True)
    name = fields.Char(readonly=True)
