import json
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

OCR_MIMETYPES = ('application/pdf',)


class ExpenseOcrResult(models.Model):
    """Résultat de la lecture d'un justificatif.

    Rangé dans un objet à part plutôt que sur la pièce : account.move n'a pas à porter les données du service
    OCR. Une lecture par justificatif (somme de contrôle), ce qui évite de payer deux fois le même fichier.
    """
    _name = 'expense.ocr.result'
    _description = "Résultat de lecture d'un justificatif"
    _order = 'id desc'
    _sql_constraints = [
        ('move_checksum_uniq', 'unique (move_id, checksum)', "Ce justificatif a déjà été lu pour cette pièce."),
    ]

    move_id = fields.Many2one('account.move', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='move_id.company_id', store=True)
    attachment_id = fields.Many2one('ir.attachment', ondelete='set null')
    checksum = fields.Char(required=True)
    state = fields.Selection(
        [('pending', "À lire"), ('done', "Lu"), ('error', "Erreur")], default='pending', required=True, index=True)
    provider = fields.Char()
    payload = fields.Text(help="Résultat normalisé du service OCR, en JSON.")
    confidence = fields.Float()
    error = fields.Text()
    warnings = fields.Text(help="Remarques à l'attention du salarié, une par ligne (taxe absente, écart de total...).")

    @api.model
    def _is_readable(self, attachment):
        mimetype = attachment.mimetype or ''
        return mimetype.startswith('image/') or mimetype in OCR_MIMETYPES

    def action_read(self):
        """Lit les justificatifs en attente ; une erreur est notée sur le résultat et n'interrompt pas les autres."""
        for result in self.filtered(lambda r: r.state == 'pending'):
            move = result.move_id
            provider = self.env['expense.ocr']._get_param('provider')
            try:
                attachment = result.attachment_id.sudo()
                normalized = self.env['expense.ocr'].analyze(attachment.raw, attachment.mimetype)
            except Exception as error:  # noqa: BLE001 - un service externe peut échouer de multiples façons
                _logger.warning("Lecture OCR en échec pour la pièce %s : %s", move.id, error)
                result.sudo().write({'state': 'error', 'error': str(error), 'provider': provider})
                continue
            payload = dict(normalized, date=normalized['date'].isoformat() if normalized.get('date') else None)
            result.sudo().write({
                'state': 'done', 'provider': provider, 'error': False,
                'payload': json.dumps(payload, ensure_ascii=False), 'confidence': normalized.get('confidence', 0.0),
            })
            notes = move._apply_ocr_result(normalized)
            result.sudo().warnings = '\n'.join(notes) if notes else False
