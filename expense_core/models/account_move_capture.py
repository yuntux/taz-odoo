import base64
import binascii
import logging
from datetime import datetime

from odoo import api, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools.mimetypes import guess_mimetype

from .account_move import EXPENSE_ORIGINS, OFFICE_GROUP

_logger = logging.getLogger(__name__)
CAPTURE_MAX_BYTES = 8 * 1024 * 1024
CAPTURE_EXTENSIONS = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'}


class AccountMove(models.Model):
    _inherit = 'account.move'

    @api.model
    def _expense_decode_photo(self, image_b64, filename=None):
        """Photo reçue de l'écran de capture : (contenu, type, nom) après contrôle ; refuse ce qui n'est pas une image
        raisonnable."""
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
        name = filename or 'ticket_%s.%s' % (datetime.now().strftime('%Y%m%d_%H%M%S'), CAPTURE_EXTENSIONS[mimetype])
        return raw, mimetype, name

    def _expense_open_action(self):
        """Action qui ouvre la dépense sur l'écran simplifié du reçu."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.id,
            'view_mode': 'form',
            'views': [(self.env.ref('expense_core.view_expense_receipt_form').id, 'form')],
            'target': 'current',
        }

    @api.model
    def expense_create_from_photo(self, image_b64, filename=None):
        """Point d'entrée de l'écran « Photographier un ticket » : crée le reçu en brouillon de l'utilisateur, y joint
        la photo et lance la lecture OCR ; retourne l'action qui ouvre le reçu.

        L'utilisateur n'a pas le droit de créer librement une pièce jointe : elle est posée avec les droits du
        système, uniquement sur le reçu que cet appel vient de créer pour lui.
        """
        raw, mimetype, name = self._expense_decode_photo(image_b64, filename)
        move = self.create({'move_type': 'in_invoice', 'expense_origin': 'employee_receipt'})
        if not move.partner_id:
            raise UserError(_(
                "Votre utilisateur n'est lié à aucune fiche employé avec un contact de travail dans cette société : "
                "demandez au gestionnaire des notes de frais de la compléter."))
        move._expense_store_photo(raw, mimetype, name)
        return move._expense_open_action()

    @api.model
    def expense_attach_photo(self, move_id, image_b64, filename=None):
        """Joint la photo à une dépense qui existe déjà (dépense par carte à justifier) : seul celui qui dépense, ou
        l'Office, peut le faire, et seulement tant que la dépense est en brouillon. Retourne l'action qui l'ouvre."""
        move = self.sudo().browse(move_id).exists()
        if not move or move.expense_origin not in EXPENSE_ORIGINS or move.state != 'draft':
            raise UserError(_("Cette dépense n'accepte plus de justificatif."))
        spender = move._get_expense_spender().user_id
        if spender != self.env.user and not self.env.user.has_group(OFFICE_GROUP):
            raise AccessError(_("Seul le porteur de la carte ou l'Office peut ajouter un justificatif à cette dépense."))
        raw, mimetype, name = self._expense_decode_photo(image_b64, filename)
        move._expense_store_photo(raw, mimetype, name)
        return move.with_user(self.env.user)._expense_open_action()

    def _expense_store_photo(self, raw, mimetype, name):
        """Pose la photo en pièce jointe (avec les droits du système : l'utilisateur ne peut pas créer librement une
        pièce jointe) et lance la lecture OCR, dont l'échec ne fait pas perdre la photo."""
        self.ensure_one()
        self.env['ir.attachment'].sudo().create({
            'name': name, 'raw': raw, 'mimetype': mimetype, 'res_model': 'account.move', 'res_id': self.id})
        try:
            self.action_run_ocr()
        except Exception:  # noqa: BLE001 - la photo est déjà enregistrée : l'utilisateur pourra relancer la lecture
            _logger.exception("Lecture OCR de la dépense %s après prise de photo", self.id)

    def action_expense_add_receipt_photo(self):
        """Bouton « Ajouter le justificatif (photo) » : ouvre l'écran de capture, qui joindra la photo à cette dépense."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client', 'tag': 'expense_core.receipt_capture', 'target': 'current',
            'name': _("Justificatif de la dépense"), 'context': {'expense_move_id': self.id}}
