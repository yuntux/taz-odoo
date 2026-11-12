from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    expense_payment_method_line_id = fields.Many2one(
        related='company_id.expense_payment_method_line_id', readonly=False,
    )

    expense_reimbursement_method_line_id = fields.Many2one(
        related='company_id.expense_reimbursement_method_line_id', readonly=False,
    )

    # Service OCR : réglage commun à toutes les sociétés, stocké en paramètres système (réservés aux
    # administrateurs, y compris la clé d'API).
    expense_ocr_provider = fields.Selection(
        [('azure', "Azure Document Intelligence")], string="Service OCR",
        config_parameter='expense_core.ocr_provider',
        help="Service qui lit les justificatifs (tickets, factures PDF). Vide : la saisie reste manuelle.")
    expense_ocr_endpoint = fields.Char(
        string="Adresse du service OCR", config_parameter='expense_core.ocr_endpoint',
        help="Pour Azure : l'endpoint de la ressource, par exemple https://<nom>.cognitiveservices.azure.com")
    expense_ocr_api_key = fields.Char(
        string="Clé du service OCR", config_parameter='expense_core.ocr_api_key',
        help="Clé d'API du service OCR, lisible des seuls administrateurs système.")
    expense_ocr_model = fields.Char(
        string="Modèle OCR", config_parameter='expense_core.ocr_model', default='prebuilt-receipt',
        help="Pour Azure : prebuilt-receipt (tickets), prebuilt-invoice (factures).")
