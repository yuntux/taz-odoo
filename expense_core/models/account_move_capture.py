import base64
import binascii
import logging
from datetime import datetime

from odoo import api, models, _
from odoo.exceptions import UserError
from odoo.tools.mimetypes import guess_mimetype

_logger = logging.getLogger(__name__)
CAPTURE_MAX_BYTES = 8 * 1024 * 1024
CAPTURE_EXTENSIONS = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}


class AccountMove(models.Model):
    _inherit = 'account.move'

    @api.model
    def expense_create_from_photo(self, image_b64, filename=None):
        """Point d'entrée de l'écran « Photographier un ticket » : crée le reçu en brouillon de l'utilisateur, y joint
        la photo et lance la lecture OCR ; retourne l'action qui ouvre le reçu.

        L'utilisateur n'a pas le droit de créer librement une pièce jointe : elle est posée avec les droits du
        système, uniquement sur le reçu que cet appel vient de créer pour lui.
        """
        try:
            raw = base64.b64decode(image_b64 or '', validate=True)
        except (binascii.Error, ValueError):
            raise UserError(_("La photo est illisible : reprenez-la."))
        if not raw:
            raise UserError(_("La photo est vide : reprenez-la."))
        if len(raw) > CAPTURE_MAX_BYTES:
            raise UserError(_("La photo est trop volumineuse (8 Mo au plus) : reprenez-la de moins près."))
        mimetype = guess_mimetype(raw)
        if mimetype not in CAPTURE_EXTENSIONS:
            raise UserError(_("Le fichier n'est pas une image (JPEG, PNG ou WebP attendu)."))

        move = self.create({'move_type': 'in_invoice', 'expense_origin': 'employee_receipt'})
        if not move.partner_id:
            raise UserError(_(
                "Votre utilisateur n'est lié à aucune fiche employé avec un contact de travail dans cette société : "
                "demandez au gestionnaire des notes de frais de la compléter."))
        name = filename or 'ticket_%s.%s' % (datetime.now().strftime('%Y%m%d_%H%M%S'), CAPTURE_EXTENSIONS[mimetype])
        self.env['ir.attachment'].sudo().create({
            'name': name, 'raw': raw, 'mimetype': mimetype, 'res_model': 'account.move', 'res_id': move.id})
        try:
            move.action_run_ocr()
        except Exception:  # noqa: BLE001 - la photo est déjà enregistrée : l'utilisateur pourra relancer la lecture
            _logger.exception("Lecture OCR du reçu %s après prise de photo", move.id)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': move.id,
            'view_mode': 'form',
            'views': [(self.env.ref('expense_core.view_expense_receipt_form').id, 'form')],
            'target': 'current',
        }
