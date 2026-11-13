from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    statement_import_api_login = fields.Char(related='company_id.statement_import_api_login', readonly=False,
                                             groups='base.group_system')
    statement_import_api_password = fields.Char(related='company_id.statement_import_api_password', readonly=False,
                                                groups='base.group_system')
    qonto_webhook_secret = fields.Char(related='company_id.qonto_webhook_secret', readonly=False,
                                       groups='base.group_system')
    qonto_default_partner_id = fields.Many2one(related='company_id.qonto_default_partner_id', readonly=False)
    qonto_reminder_days = fields.Integer(related='company_id.qonto_reminder_days', readonly=False)
