from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    sage_api_endpoint = fields.Char(related='company_id.sage_api_endpoint', readonly=False)
    sage_api_token = fields.Char(related='company_id.sage_api_token', readonly=False)
    sage_contract_code = fields.Char(related='company_id.sage_contract_code', readonly=False)
    sage_etablissement_code = fields.Char(related='company_id.sage_etablissement_code', readonly=False)
    sage_compte_equilibre = fields.Char(related='company_id.sage_compte_equilibre', readonly=False)
