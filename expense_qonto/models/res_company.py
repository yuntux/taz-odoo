import secrets

from odoo import _, fields, models
from odoo.exceptions import UserError


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
    qonto_webhook_subscription_id = fields.Char(
        string="Qonto : abonnement au webhook", groups='base.group_system', copy=False, readonly=True,
        help="Identifiant de l'abonnement créé chez Qonto par le bouton « Inscrire le webhook ».")

    def _qonto_webhook_url(self):
        self.ensure_one()
        base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        return base + '/expense_qonto/webhook'

    def action_qonto_register_webhook(self):
        """Inscrit le webhook chez Qonto (événements v1/transactions) et garde le secret de signature."""
        self.ensure_one()
        self = self.sudo()
        url = self._qonto_webhook_url()
        if not url.startswith('https://'):
            raise UserError(_("Qonto n'accepte qu'une adresse en HTTPS ; l'adresse d'Odoo (web.base.url) est : %s", url))
        if self.qonto_webhook_subscription_id:
            raise UserError(_("Le webhook est déjà inscrit (abonnement %s) : supprimez-le d'abord pour le recréer.",
                              self.qonto_webhook_subscription_id))
        secret = secrets.token_urlsafe(48)  # 64 caractères : Qonto en demande de 32 à 128
        subscription = self.env['qonto.api'].create_webhook_subscription(self, url, secret, ['v1/transactions'])
        self.write({'qonto_webhook_secret': secret, 'qonto_webhook_subscription_id': subscription.get('id') or 'inconnu'})
        return True

    def action_qonto_unregister_webhook(self):
        self.ensure_one()
        self = self.sudo()
        if self.qonto_webhook_subscription_id and self.qonto_webhook_subscription_id != 'inconnu':
            self.env['qonto.api'].delete_webhook_subscription(self, self.qonto_webhook_subscription_id)
        self.write({'qonto_webhook_secret': False, 'qonto_webhook_subscription_id': False})
        return True
