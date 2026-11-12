from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    expense_fiscal_position_id = fields.Many2one(
        'account.fiscal.position', string="Position fiscale des frais", company_dependent=True,
        help="Pour une catégorie de dépense : la position fiscale qui donne le régime de TVA de la dépense "
             "(déductible, carburant, non déductible...). Elle remplace la taxe de chaque ligne par celle du "
             "régime, au même taux : une ligne à 10 % reste à 10 %.",
    )
