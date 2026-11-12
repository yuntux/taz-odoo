import logging

from odoo import api, models
from odoo.tools.misc import format_amount

logger = logging.getLogger(__name__)


class AccountInvoiceImport(models.TransientModel):
    _inherit = "account.invoice.import"

    def prepare_facturx_xpath_dict(self):
        xpathd = super().prepare_facturx_xpath_dict()
        # BT-13 (référence de commande) : la clé "origin" alimente account.move.invoice_origin
        xpathd["origin"] = [
            "//ram:ApplicableHeaderTradeAgreement"
            "/ram:BuyerOrderReferencedDocument"
            "/ram:IssuerAssignedID"
        ]
        # BT-11 (référence de projet) : rapprochée du numéro de projet dans _post_process_invoice
        xpathd["project_reference"] = [
            "//ram:ApplicableHeaderTradeAgreement"
            "/ram:SpecifiedProcurementProject"
            "/ram:ID"
        ]
        return xpathd

    @api.model
    def _find_project_from_references(self, parsed_inv):
        """BT-11 en priorité, sinon BT-13 : le contenu est comparé à project.project.number
        (unique en base). Une valeur absente, inconnue ou à plusieurs numéros ne donne aucun projet."""
        for key in ("project_reference", "origin"):
            value = (parsed_inv.get(key) or "").strip()
            if not value:
                continue
            project = self.env["project.project"].search([("number", "=", value)], limit=1)
            if project:
                return project
        return self.env["project.project"]

    @api.model
    def _import_warnings(self, parsed_inv, invoice):
        """Alertes à afficher en bandeau sur la facture importée : taxe du fichier introuvable dans Odoo, total
        ou TVA différents de ceux de la facture reçue.

        L'import ne s'arrête pas dans ces cas : la ligne reçoit la taxe par défaut du produit ou du fournisseur et
        l'explication part dans le chatter, où elle passe inaperçue. Le bandeau la rend visible à l'Office.
        """
        warnings = []
        file_rates = set()
        for line in parsed_inv.get("lines") or []:
            for tax in line.get("taxes") or []:
                if tax.get("amount_type", "percent") == "percent" and tax.get("amount") is not None:
                    file_rates.add(round(float(tax["amount"]), 4))
        invoice_rates = {
            round(rate, 4)
            for rate in invoice.invoice_line_ids.filtered(lambda line: line.display_type == "product")
            .tax_ids.filtered(lambda tax: tax.amount_type == "percent")
            .mapped("amount")
        }
        for rate in sorted(file_rates - invoice_rates):
            warnings.append(
                self.env._(
                    "La facture reçue comporte une taxe à %(rate)s %%, qui n'existe pas ou n'est pas activée dans "
                    "Odoo : contactez l'Office pour l'activer. Les lignes concernées ont reçu la taxe par défaut "
                    "du produit ou du fournisseur.",
                    rate=f"{rate:g}",
                )
            )
        currency = invoice.currency_id
        file_total = parsed_inv.get("amount_total")
        if file_total is not None and currency.compare_amounts(invoice.amount_total, file_total):
            warnings.append(
                self.env._(
                    "Le total de la facture reçue (%(file_total)s) diffère de celui calculé par Odoo (%(total)s).",
                    file_total=format_amount(self.env, file_total, currency),
                    total=format_amount(self.env, invoice.amount_total, currency),
                )
            )
        elif file_total is not None and parsed_inv.get("amount_untaxed") is not None:
            file_tax = file_total - parsed_inv["amount_untaxed"]
            if currency.compare_amounts(invoice.amount_tax, file_tax):
                warnings.append(
                    self.env._(
                        "La TVA de la facture reçue (%(file_tax)s) diffère de celle calculée par Odoo (%(tax)s).",
                        file_tax=format_amount(self.env, file_tax, currency),
                        tax=format_amount(self.env, invoice.amount_tax, currency),
                    )
                )
        return warnings

    @api.model
    def _post_process_invoice(self, parsed_inv, import_config, invoice):
        res = super()._post_process_invoice(parsed_inv, import_config, invoice)
        if parsed_inv.get("type") in ("out_invoice", "out_refund"):
            return res
        warnings = self._import_warnings(parsed_inv, invoice)
        if warnings:
            invoice.einvoice_import_warning = "\n".join(warnings)
        project = self._find_project_from_references(parsed_inv)
        if project and project.account_id:
            # Seules les lignes sans distribution analytique sont renseignées : la facture reçue
            # (quantités, prix, unités) n'est pas modifiée ; rel_project_ids de la facture se remplit.
            lines = invoice.invoice_line_ids.filtered(
                lambda line: line.display_type == "product" and not line.analytic_distribution
            )
            lines.write({"analytic_distribution": {str(project.account_id.id): 100}})
            invoice.message_post(
                body=self.env._(
                    "Project %(project)s deduced from the supplier references (BT-11 / BT-13).",
                    project=project.display_name,
                )
            )
        return res
