from odoo import fields, models


class QontoOauthScope(models.Model):
    """Étendue (scope) OAuth 2.0 que l'on peut demander à Qonto ; la liste proposée est celle de data/qonto_oauth_scope_data.xml."""
    _name = 'qonto.oauth.scope'
    _description = "Étendue OAuth 2.0 de Qonto"
    _order = 'code'
    _rec_name = 'code'

    code = fields.Char(string="Étendue", required=True, help="Nom de l'étendue chez Qonto, par exemple organization.read.")
    description = fields.Char(string="Ce qu'elle donne")

    _sql_constraints = [('code_unique', 'unique(code)', "Cette étendue existe déjà.")]
