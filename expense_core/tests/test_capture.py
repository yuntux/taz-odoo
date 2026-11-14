import base64
import io

from PIL import Image

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.addons.expense_core.models import account_move_capture


def _png_b64(color=(255, 255, 255)):
    buffer = io.BytesIO()
    Image.new('RGB', (40, 60), color).save(buffer, format='PNG')
    return base64.b64encode(buffer.getvalue()).decode()


@tagged('post_install', '-at_install')
class TestReceiptCapture(TransactionCase):
    """Écran « Photographier un ticket » : le serveur crée le reçu en brouillon, y joint la photo, renvoie l'action."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        ref = cls.env.ref
        cls.user = cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Photographe test', 'login': 'photo_test', 'email': 'photo@example.com',
            'groups_id': [Command.set([ref('base.group_user').id, ref('taz-common.taz-basic').id])],
        })
        cls.employee = cls.env['hr.employee'].create({'name': 'Photographe test', 'user_id': cls.user.id})

    def _call(self, image, filename=None, user=None):
        return self.env['account.move'].with_user(user or self.user).expense_create_from_photo(image, filename)

    def test_photo_creates_a_draft_receipt_with_the_picture(self):
        action = self._call(_png_b64(), 'ticket.png')
        move = self.env['account.move'].browse(action['res_id'])
        self.assertEqual((move.state, move.move_type, move.expense_origin), ('draft', 'in_invoice', 'employee_receipt'))
        self.assertEqual(move.partner_id, self.employee.work_contact_id)
        attachment = self.env['ir.attachment'].search([('res_model', '=', 'account.move'), ('res_id', '=', move.id)])
        self.assertEqual(attachment.name, 'ticket.png')
        self.assertEqual(attachment.mimetype, 'image/png')
        self.assertEqual(action['res_model'], 'account.move')
        self.assertEqual(action['views'], [(self.env.ref('expense_core.view_expense_receipt_form').id, 'form')])

    def test_default_name_has_the_extension_of_the_image(self):
        move = self.env['account.move'].browse(self._call(_png_b64())['res_id'])
        attachment = self.env['ir.attachment'].search([('res_model', '=', 'account.move'), ('res_id', '=', move.id)])
        self.assertTrue(attachment.name.startswith('ticket_') and attachment.name.endswith('.png'), attachment.name)

    def test_the_employee_sees_the_receipt_and_the_picture(self):
        move = self.env['account.move'].browse(self._call(_png_b64())['res_id']).with_user(self.user)
        self.assertEqual(move.read(['state'])[0]['state'], 'draft')

    def test_not_an_image_is_refused(self):
        with self.assertRaises(UserError):
            self._call(base64.b64encode(b'%PDF-1.4 pas une image').decode())

    def test_garbage_and_empty_are_refused(self):
        for value in ('###', '', None):
            with self.assertRaises(UserError):
                self._call(value)

    def test_too_big_is_refused(self):
        original = account_move_capture.CAPTURE_MAX_BYTES
        account_move_capture.CAPTURE_MAX_BYTES = 10
        try:
            with self.assertRaises(UserError):
                self._call(_png_b64())
        finally:
            account_move_capture.CAPTURE_MAX_BYTES = original

    def test_user_without_employee_gets_a_clear_error(self):
        stranger = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Sans fiche', 'login': 'photo_nobody', 'email': 'nobody@example.com',
            'groups_id': [Command.set([self.env.ref('base.group_user').id, self.env.ref('taz-common.taz-basic').id])],
        })
        with self.assertRaises(UserError):
            self._call(_png_b64(), user=stranger)

    def test_reading_fills_a_receipt_created_without_any_line(self):
        """Le reçu créé par la photo n'a pas de ligne : la lecture doit en créer, comme sur le formulaire classique."""
        move = self.env['account.move'].browse(self._call(_png_b64())['res_id'])
        self.assertFalse(move.invoice_line_ids)
        move._apply_ocr_result({
            'merchant': 'BISTROT DES DUCS', 'date': '2026-11-13', 'total': 94.7, 'tax_total': None, 'currency': 'EUR',
            'lines': [{'name': 'MENU', 'amount': 94.7, 'tax_rate': None}],
            'tax_details': [{'rate': 0.1, 'net': 72.45, 'tax': 7.25}, {'rate': 0.2, 'net': 12.5, 'tax': 2.5}],
            'confidence': 0.98,
        })
        lines = move.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        self.assertEqual(len(lines), 2)
        self.assertEqual(str(move.invoice_date), '2026-11-13')
        self.assertAlmostEqual(sum(lines.mapped('price_unit')), 94.7, places=1)
