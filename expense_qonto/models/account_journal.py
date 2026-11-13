from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    # Noms repris du module Akretion account_statement_import_api.
    statement_import_api_account_identifier = fields.Char(
        string="Identifiant du compte Qonto", copy=False,
        help="Identifiant du compte bancaire chez Qonto : il relie les transactions reçues à ce journal.")
    statement_import_api_last_success = fields.Datetime(
        string="Dernière lecture Qonto réussie", copy=False, readonly=True,
        help="Fin de la dernière lecture planifiée : la suivante reprend une heure avant.")
