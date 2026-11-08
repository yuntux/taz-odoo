from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Champs Odoo requis avant de pouvoir créer un tiers sur Sage (§8.2) -- en plus d'y aider la
# cascade de recherche future (§8.1), les envoyer vide créerait une fiche Sage incomplète.
_SAGE_CREATE_REQUIRED_FIELDS = [
    ('siret', "SIRET"),
    ('siren', "SIREN"),
    ('vat', "TVA intracommunautaire"),
]


class SagePartnerResolveWizard(models.TransientModel):
    """Un seul rôle (client ou fournisseur) d'un seul tiers (§8.0 point 2), déclenché quand la
    cascade de recherche (`res.partner._sage_resolve_role`) ne peut pas conclure toute seule :
    soit plusieurs candidats trouvés (`mode='choose'`), soit aucun (`mode='create'`).
    """
    _name = 'sage.partner.resolve.wizard'
    _description = "Résolution d'un tiers sur Sage"

    partner_id = fields.Many2one('res.partner', required=True, readonly=True)
    role = fields.Selection([
        ('client', 'Client'),
        ('vendor', 'Fournisseur'),
    ], required=True, readonly=True)
    mode = fields.Selection([
        ('choose', 'Choisir un candidat'),
        ('create', 'Créer un nouveau tiers'),
    ], required=True, readonly=True)
    candidate_ids = fields.One2many(
        'sage.partner.resolve.wizard.candidate', 'wizard_id', readonly=True,
        string="Tiers trouvés sur Sage",
    )
    selected_candidate_id = fields.Many2one(
        'sage.partner.resolve.wizard.candidate', string="Rattacher ce tiers existant",
        domain="[('wizard_id', '=', id)]",
    )
    new_code = fields.Char(string="Code du nouveau tiers")
    missing_required_label = fields.Char(compute='_compute_missing_required_label')
    can_validate = fields.Boolean(compute='_compute_missing_required_label')

    @api.depends('mode', 'partner_id.siret', 'partner_id.siren', 'partner_id.vat')
    def _compute_missing_required_label(self):
        for wizard in self:
            missing = wizard._sage_missing_required_fields()
            wizard.missing_required_label = ", ".join(missing) if missing else False
            wizard.can_validate = not missing

    def _sage_missing_required_fields(self):
        self.ensure_one()
        if self.mode != 'create':
            return []
        return [label for field_name, label in _SAGE_CREATE_REQUIRED_FIELDS if not self.partner_id[field_name]]

    def action_validate(self):
        self.ensure_one()
        field_name = 'sage_client_code' if self.role == 'client' else 'sage_vendor_code'
        if self.partner_id[field_name]:
            raise UserError(_(
                "%(partner)s a déjà un code sur Sage pour ce rôle -- probablement résolu "
                "entre-temps. Fermez ce wizard sans le valider.",
                partner=self.partner_id.display_name,
            ))

        if self.mode == 'choose':
            if not self.selected_candidate_id:
                raise UserError(_("Sélectionnez un tiers dans la liste."))
            self.partner_id.write({field_name: self.selected_candidate_id.code})
        else:
            missing = self._sage_missing_required_fields()
            if missing:
                raise UserError(_(
                    "Impossible de créer ce tiers sur Sage : %(missing)s manquant(s) sur sa fiche "
                    "Odoo. Complétez la fiche avant de réessayer.",
                    missing=", ".join(missing),
                ))
            if not self.new_code:
                raise UserError(_("Saisissez un code pour créer le tiers."))
            self.partner_id._sage_create_role(self.role, code=self.new_code)

        return {'type': 'ir.actions.act_window_close'}


class SagePartnerResolveWizardCandidate(models.TransientModel):
    _name = 'sage.partner.resolve.wizard.candidate'
    _description = "Candidat tiers Sage trouvé par la cascade de recherche"

    wizard_id = fields.Many2one('sage.partner.resolve.wizard', required=True, ondelete='cascade')
    code = fields.Char(readonly=True)
    name = fields.Char(readonly=True)

    def _compute_display_name(self):
        for candidate in self:
            candidate.display_name = "%s (%s)" % (candidate.code, candidate.name) if candidate.name else candidate.code
