from odoo import Command
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestImportWarnings(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.supplier = cls.env["res.partner"].create({"name": "Fournisseur import", "is_company": True})
        cls.tax20 = cls.env["account.tax"].create(
            {"name": "TVA 20 % import", "amount": 20, "type_tax_use": "purchase", "company_id": cls.company.id}
        )

    def _invoice(self, price=100.0):
        return self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "partner_id": self.supplier.id,
                "invoice_date": "2026-11-10",
                "invoice_line_ids": [
                    Command.create({"name": "Prestation", "quantity": 1, "price_unit": price, "tax_ids": [Command.set(self.tax20.ids)]})
                ],
            }
        )

    def _parsed(self, rate=20.0, untaxed=100.0, total=120.0):
        return {
            "lines": [{"taxes": [{"amount_type": "percent", "amount": rate}]}],
            "amount_untaxed": untaxed,
            "amount_total": total,
        }

    def _warnings(self, parsed, invoice):
        return self.env["account.invoice.import"]._import_warnings(parsed, invoice)

    def test_consistent_invoice_has_no_warning(self):
        self.assertEqual(self._warnings(self._parsed(), self._invoice()), [])

    def test_unknown_tax_rate_is_reported_with_who_to_contact(self):
        warnings = self._warnings(self._parsed(rate=8.5, total=108.5), self._invoice())
        self.assertTrue(any("taxe à 8.5 %" in w and "contactez l'Office" in w for w in warnings))

    def test_total_difference_is_reported(self):
        warnings = self._warnings(self._parsed(total=121.0), self._invoice())
        self.assertTrue(any("total de la facture reçue" in w for w in warnings))

    def test_vat_difference_is_reported_when_the_total_matches(self):
        # total identique mais TVA différente : 100 HT + 20 de TVA côté Odoo, 101 HT + 19 de TVA côté fichier
        warnings = self._warnings(self._parsed(untaxed=101.0, total=120.0), self._invoice())
        self.assertTrue(any("TVA de la facture reçue" in w for w in warnings))

    def test_warning_is_stored_and_the_view_loads(self):
        invoice = self._invoice()
        invoice.einvoice_import_warning = "x"
        self.assertEqual(invoice.einvoice_import_warning, "x")
        arch = self.env["account.move"].get_view(self.env.ref("account.view_move_form").id)["arch"]
        self.assertIn("einvoice_import_warning", arch if isinstance(arch, str) else arch.decode())
