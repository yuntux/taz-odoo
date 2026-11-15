import hmac
import secrets
from datetime import timedelta

from odoo import _, fields, models
from odoo.exceptions import UserError

# Étendues demandées par défaut : gérer les webhooks, lire les transactions et lire les cartes.
DEFAULT_OAUTH_SCOPES = ('offline_access', 'webhook', 'organization.read', 'card.read')


class ResCompany(models.Model):
    _inherit = 'res.company'

    # Noms et droits repris du module Akretion account_statement_import_api (login, password).
    statement_import_api_login = fields.Char(
        string="Qonto : identifiant", groups='base.group_system', copy=False,
        help="Identifiant de l'organisation (Qonto > Intégrations et partenariats > Clés API).")
    statement_import_api_password = fields.Char(
        string="Qonto : clé secrète", groups='base.group_system', copy=False)
    qonto_environment = fields.Selection(
        [('production', "Environnement de production"), ('sandbox', "Environnement bac à sable")],
        string="Qonto : environnement", default='production', required=True, groups='base.group_system',
        help="Le bac à sable appelle l'API de test de Qonto (thirdparty-sandbox.staging.qonto.co), avec le jeton de "
             "staging, au lieu de la production.")
    qonto_staging_token = fields.Char(
        string="Qonto : jeton de staging", groups='base.group_system', copy=False,
        help="Jeton personnel du portail développeur de Qonto, envoyé dans l'en-tête X-Qonto-Staging-Token. "
             "Obligatoire dans l'environnement bac à sable.")
    qonto_auth_mode = fields.Selection(
        [('oauth', "OAuth 2.0"), ('api_key', "Clé d'API")], string="Qonto : mode de connexion", default='oauth',
        required=True, groups='base.group_system',
        help="L'inscription du webhook n'est possible qu'en OAuth 2.0 : la clé d'API de Qonto ne donne accès qu'à la "
             "lecture des transactions (lecture planifiée).")
    qonto_oauth_client_id = fields.Char(string="Qonto : identifiant client OAuth", groups='base.group_system', copy=False)
    qonto_oauth_client_secret = fields.Char(string="Qonto : secret client OAuth", groups='base.group_system', copy=False)
    qonto_oauth_scope_ids = fields.Many2many(
        'qonto.oauth.scope', 'res_company_qonto_oauth_scope_rel', 'company_id', 'scope_id',
        string="Qonto : étendues demandées", groups='base.group_system',
        default=lambda self: self.env['qonto.oauth.scope'].search([('code', 'in', DEFAULT_OAUTH_SCOPES)]),
        help="Droits demandés à Qonto à la connexion. Les étendues doivent aussi être activées sur l'application OAuth "
             "du portail développeur. Par défaut : webhooks, lecture des transactions et des cartes ; offline_access "
             "(jeton de renouvellement) est toujours ajoutée.")
    qonto_oauth_redirect_uri = fields.Char(
        string="Qonto : adresse de redirection", compute='_compute_qonto_oauth_redirect_uri',
        groups='base.group_system',
        help="À enregistrer telle quelle sur l'application OAuth du portail développeur de Qonto.")
    qonto_oauth_state = fields.Char(groups='base.group_system', copy=False)
    qonto_access_token = fields.Char(string="Qonto : jeton d'accès", groups='base.group_system', copy=False)
    qonto_access_token_expiry = fields.Datetime(groups='base.group_system', copy=False)
    qonto_refresh_token = fields.Char(string="Qonto : jeton de renouvellement", groups='base.group_system', copy=False)
    qonto_oauth_connected_on = fields.Datetime(
        string="Qonto : connecté le", groups='base.group_system', copy=False, readonly=True,
        help="Date de la dernière connexion OAuth. Le jeton de renouvellement dure 90 jours : au-delà de la dernière "
             "utilisation il faut se reconnecter.")
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

    def _compute_qonto_oauth_redirect_uri(self):
        base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        for company in self:
            company.qonto_oauth_redirect_uri = base + '/expense_qonto/oauth/callback'

    def _qonto_oauth_scope_codes(self):
        """Étendues demandées : celles de la société (par défaut webhooks, transactions, cartes), plus offline_access,
        sans laquelle Qonto ne donne pas de jeton de renouvellement."""
        self.ensure_one()
        Scope = self.env['qonto.oauth.scope'].sudo()
        scopes = self.sudo().qonto_oauth_scope_ids or Scope.search([('code', 'in', DEFAULT_OAUTH_SCOPES)])
        return sorted(set(scopes.mapped('code')) | {'offline_access'})

    def action_qonto_oauth_connect(self):
        """Lance la connexion OAuth 2.0 : envoie le navigateur de l'administrateur sur la page d'autorisation de Qonto,
        qui le ramène sur /expense_qonto/oauth/callback avec un code."""
        self.ensure_one()
        self = self.sudo()
        if not (self.qonto_oauth_client_id and self.qonto_oauth_client_secret):
            raise UserError(_("Renseignez l'identifiant et le secret client OAuth de l'application du portail développeur."))
        if not self.qonto_oauth_redirect_uri.startswith('https://'):
            raise UserError(_("Qonto n'accepte qu'une adresse de redirection en HTTPS ; l'adresse d'Odoo (web.base.url) "
                              "donne : %s", self.qonto_oauth_redirect_uri))
        # L'état lie le retour de Qonto à cette société ; il est à usage unique.
        state = '%s-%s' % (self.id, secrets.token_urlsafe(32))
        self.qonto_oauth_state = state
        url = self.env['qonto.api']._oauth_authorization_url(self, state)
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'self'}

    def _qonto_oauth_finish(self, code, state):
        """Fin de la connexion : vérifie l'état puis échange le code contre les jetons, qui sont gardés."""
        self.ensure_one()
        self = self.sudo()
        expected = self.qonto_oauth_state or ''
        if not (expected and state and hmac.compare_digest(expected, state)):
            raise UserError(_("La réponse de Qonto ne correspond à aucune connexion en cours : recommencez la connexion."))
        self.qonto_oauth_state = False
        tokens = self.env['qonto.api']._oauth_exchange_code(self, code)
        self._qonto_store_tokens(tokens)
        self.qonto_oauth_connected_on = fields.Datetime.now()
        return True

    def _qonto_store_tokens(self, tokens):
        """Garde les jetons reçus de Qonto. Le jeton de renouvellement est à usage unique : le nouveau remplace
        l'ancien."""
        self.ensure_one()
        self = self.sudo()
        expires_in = int(tokens.get('expires_in') or 3600)
        vals = {
            'qonto_access_token': tokens.get('access_token'),
            'qonto_access_token_expiry': fields.Datetime.now() + timedelta(seconds=expires_in),
        }
        if tokens.get('refresh_token'):
            vals['qonto_refresh_token'] = tokens['refresh_token']
        self.write(vals)

    def action_qonto_oauth_disconnect(self):
        self.ensure_one()
        self.sudo().write({
            'qonto_access_token': False, 'qonto_access_token_expiry': False, 'qonto_refresh_token': False,
            'qonto_oauth_connected_on': False, 'qonto_oauth_state': False})
        return True

    def action_qonto_register_webhook(self):
        """Inscrit le webhook chez Qonto (événements v1/transactions) et garde le secret de signature."""
        self.ensure_one()
        self = self.sudo()
        if self.qonto_auth_mode != 'oauth':
            raise UserError(_("Qonto n'accepte l'inscription d'un webhook qu'en OAuth 2.0, pas avec une clé d'API : "
                              "choisissez ce mode de connexion et connectez-vous."))
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
