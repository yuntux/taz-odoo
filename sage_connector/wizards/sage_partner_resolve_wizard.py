from odoo import _, fields, models
from odoo.exceptions import UserError


class SagePartnerResolveWizard(models.TransientModel):
    """Une ligne du wizard de résolution (§8.0 point 2) : soit un candidat trouvé sur Sage (par
    SIRET/TVA en écart, ou par recherche nominative) pour un rôle (client/fournisseur) d'un tiers,
    soit l'option "créer un nouveau tiers" quand aucun candidat n'a été trouvé, soit une ligne
    informative pour un rôle déjà résolu (non sélectionnable). Jamais de rattachement automatique
    sur un simple match de nom -- toujours une confirmation humaine ici (§8.1 critère 4).
    """
    _name = 'sage.partner.resolve.wizard'
    _description = "Ligne de résolution d'un tiers sur Sage"

    batch_id = fields.Many2one('sage.partner.resolve.batch.wizard', ondelete='cascade')
    partner_id = fields.Many2one('res.partner', required=True, readonly=True)
    role = fields.Selection([
        ('client', 'Client'),
        ('vendor', 'Fournisseur'),
    ], required=True, readonly=True)
    resolved = fields.Boolean(
        readonly=True,
        help="Ce rôle est déjà résolu sur Sage -- ligne informative, non sélectionnable.",
    )
    is_create_option = fields.Boolean(readonly=True, help="Option \"créer un nouveau tiers\".")
    code = fields.Char(readonly=True, string="Code Sage")
    name = fields.Char(readonly=True, string="Libellé")
    new_code = fields.Char(string="Code à créer")
    selected = fields.Boolean(string="Choisir")


class SagePartnerResolveBatchWizard(models.TransientModel):
    """Conteneur affiché par `res.partner.action_sage_resolve` (§8.0 point 2) : un formulaire
    classique (pied de page Valider/Annuler), une ligne par candidat/option trouvée -- qu'il y en
    ait une ou plusieurs par rôle, toujours le même formulaire.
    """
    _name = 'sage.partner.resolve.batch.wizard'
    _description = "Résolution des tiers sur Sage"

    line_ids = fields.One2many('sage.partner.resolve.wizard', 'batch_id', string="Lignes")

    def action_validate(self):
        pairs = {(line.partner_id, line.role) for line in self.line_ids if not line.resolved}
        for partner, role in pairs:
            lines = self.line_ids.filtered(
                lambda l, partner=partner, role=role: l.partner_id == partner and l.role == role and not l.resolved
            )
            selected = lines.filtered('selected')
            if len(selected) > 1:
                raise UserError(_(
                    "%(partner)s (%(role)s) : un seul tiers peut être sélectionné à la fois.",
                    partner=partner.display_name, role=dict(lines._fields['role'].selection)[role],
                ))
            if not selected:
                continue

            field_name = 'sage_client_code' if role == 'client' else 'sage_vendor_code'
            if partner[field_name]:
                raise UserError(_(
                    "%(partner)s a déjà un code %(role)s sur Sage -- probablement résolu "
                    "entre-temps. Fermez ce wizard sans le valider.",
                    partner=partner.display_name, role=dict(lines._fields['role'].selection)[role],
                ))

            if selected.is_create_option:
                if not selected.new_code:
                    raise UserError(_(
                        "Saisissez un code pour créer le tiers de %(partner)s.",
                        partner=partner.display_name,
                    ))
                partner._sage_create_role(role, code=selected.new_code)
            else:
                partner.write({field_name: selected.code})

        return {'type': 'ir.actions.act_window_close'}
