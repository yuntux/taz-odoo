from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    statement_import_api_login = fields.Char(related='company_id.statement_import_api_login', readonly=False,
                                             groups='base.group_system')
    statement_import_api_password = fields.Char(related='company_id.statement_import_api_password', readonly=False,
                                                groups='base.group_system')
    qonto_environment = fields.Selection(related='company_id.qonto_environment', readonly=False,
                                         groups='base.group_system')
    qonto_staging_token = fields.Char(related='company_id.qonto_staging_token', readonly=False,
                                      groups='base.group_system')
    qonto_auth_mode = fields.Selection(related='company_id.qonto_auth_mode', readonly=False, groups='base.group_system')
    qonto_oauth_client_id = fields.Char(related='company_id.qonto_oauth_client_id', readonly=False,
                                        groups='base.group_system')
    qonto_oauth_client_secret = fields.Char(related='company_id.qonto_oauth_client_secret', readonly=False,
                                            groups='base.group_system')
    qonto_oauth_scope_ids = fields.Many2many(related='company_id.qonto_oauth_scope_ids', readonly=False,
                                             groups='base.group_system')
    qonto_oauth_redirect_custom = fields.Char(related='company_id.qonto_oauth_redirect_custom', readonly=False,
                                              groups='base.group_system')
    qonto_oauth_redirect_uri = fields.Char(related='company_id.qonto_oauth_redirect_uri', readonly=True,
                                           groups='base.group_system')
    # Adresse de retour de Qonto collée par l'administrateur (jamais gardée) quand le navigateur n'est pas revenu ici.
    qonto_oauth_return_url = fields.Char(string="Adresse de retour de Qonto", groups='base.group_system')
    qonto_oauth_connected_on = fields.Datetime(related='company_id.qonto_oauth_connected_on', readonly=True,
                                               groups='base.group_system')
    # Généré par le bouton « Inscrire le webhook » : jamais saisi à la main.
    qonto_webhook_secret = fields.Char(related='company_id.qonto_webhook_secret', readonly=True,
                                       groups='base.group_system')
    qonto_default_partner_id = fields.Many2one(related='company_id.qonto_default_partner_id', readonly=False)
    qonto_reminder_days = fields.Integer(related='company_id.qonto_reminder_days', readonly=False)
    qonto_webhook_subscription_id = fields.Char(related='company_id.qonto_webhook_subscription_id',
                                                groups='base.group_system')

    def action_qonto_oauth_connect(self):
        self.ensure_one()
        return self.company_id.action_qonto_oauth_connect()

    def action_qonto_oauth_finish_pasted(self):
        self.ensure_one()
        self.company_id.action_qonto_oauth_finish_pasted(self.qonto_oauth_return_url)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_qonto_oauth_disconnect(self):
        self.ensure_one()
        self.company_id.action_qonto_oauth_disconnect()
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_qonto_register_webhook(self):
        self.ensure_one()
        self.company_id.action_qonto_register_webhook()
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_qonto_unregister_webhook(self):
        self.ensure_one()
        self.company_id.action_qonto_unregister_webhook()
        return {'type': 'ir.actions.client', 'tag': 'reload'}
