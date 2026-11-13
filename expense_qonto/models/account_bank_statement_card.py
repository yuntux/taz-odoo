from odoo import api, fields, models


class AccountBankStatementCard(models.Model):
    # Même modèle et mêmes champs que le module Akretion account_statement_import_in_invoice (sans misc_partner_id),
    # pour pouvoir l'installer plus tard ; les champs qonto_* et employee_id sont les nôtres.
    _name = 'account.bank.statement.card'
    _description = "Payment Card"
    _check_company_auto = True

    code = fields.Char(required=True, help="Code used in the Bank API for that payment card.")
    name = fields.Char(string="Label")
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    journal_id = fields.Many2one(
        'account.journal', string="Bank Journal", check_company=True, required=True,
        domain="[('type', '=', 'bank'), ('company_id', '=', company_id)]", ondelete='restrict')
    qonto_card_id = fields.Char(string="Identifiant Qonto", copy=False, index=True,
                                help="Identifiant complet de la carte chez Qonto ; le code n'en garde que les 4 derniers chiffres.")
    employee_id = fields.Many2one('hr.employee', string="Porteur", check_company=True,
                                  help="Salarié qui porte la carte : c'est lui qui complète les dépenses de la carte.")

    @api.depends('name', 'code')
    def _compute_display_name(self):
        for card in self:
            card.display_name = card.name or card.code

    _sql_constraints = [
        ('code_journal_unique', 'unique(code, journal_id)',
         "A card already exists in this journal with the same code."),
        ('name_company_unique', 'unique(name, company_id)',
         "This label is already used on another card in this company."),
        ('qonto_card_id_unique', 'unique(qonto_card_id, company_id)',
         "Cette carte Qonto existe déjà dans la société."),
    ]
