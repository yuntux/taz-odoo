from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    # Noms et droits repris du module Akretion account_statement_import_api (login, password).
    statement_import_api_login = fields.Char(
        string="Qonto : identifiant", groups='base.group_system', copy=False,
        help="Identifiant de l'organisation (Qonto > Intégrations et partenariats > Clés API).")
    statement_import_api_password = fields.Char(
        string="Qonto : clé secrète", groups='base.group_system', copy=False)
    qonto_webhook_secret = fields.Char(
        string="Qonto : secret du webhook", groups='base.group_system', copy=False,
        help="Secret du webhook, qui sert à vérifier la signature des appels de Qonto.")
    qonto_default_partner_id = fields.Many2one(
        'res.partner', string="Tiers des dépenses par carte",
        help="Tiers de la dépense quand aucune fiche de fournisseur ne porte le nom du commerçant. "
             "Le commerçant reste visible dans le libellé de la ligne.")
    qonto_reminder_days = fields.Integer(
        string="Délai de relance des justificatifs (jours)", default=3,
        help="Une dépense par carte sans justificatif depuis ce délai est rappelée au porteur ; après le même délai "
             "encore, au directeur de mission (ou à la direction).")
