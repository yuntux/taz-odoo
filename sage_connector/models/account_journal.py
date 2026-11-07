from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    sage_sync_date_start = fields.Date(
        string="Sage - Synchroniser à partir du",
        help="Active l'export vers Sage pour ce journal. Seules les pièces dont la date de "
             "comptabilisation est strictement postérieure à cette date sont concernées. Vide = "
             "ce journal n'est pas synchronisé avec Sage.",
    )
    sage_sync_date_end = fields.Date(
        string="Sage - Synchroniser jusqu'au",
        help="Optionnel. Si renseignée, seules les pièces dont la date de comptabilisation est "
             "strictement antérieure à cette date restent concernées (pour clore une fenêtre de "
             "synchronisation, ex. bascule vers un autre système). Vide = pas de borne haute.",
    )
    sage_journal_code = fields.Char(
        string="Sage - Code journal",
        help="Code journal attendu par Sage (champ 'codeJournal') -- ne pas supposer qu'il "
             "correspond au code journal Odoo.",
    )
