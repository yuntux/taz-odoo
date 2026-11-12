import json

import logging

from odoo import Command, api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)
APPROVAL_MOVE_TYPES = ('in_invoice',)
# Une dépense avancée par un salarié ou payée par carte est une facture fournisseur (in_invoice), comme dans hr_expense
# et comme le comprennent project_accounting et sage_connector ; expense_origin la distingue des factures de la PA.
EXPENSE_ORIGINS = ('employee_receipt', 'card')
OFFICE_GROUP = 'taz-common.taz-adv'


class AccountMove(models.Model):
    _name = 'account.move'
    _inherit = ['account.move', 'tier.validation']

    # Bon à payer : la validation par niveaux se déclenche avant le passage en « posted ».
    _state_from = ['draft']
    _state_to = ['posted']
    _cancel_state = 'cancel'
    _tier_validation_manual_config = False

    expense_origin = fields.Selection(
        [('pa_invoice', "Facture de la PA"),
         ('employee_receipt', "Reçu d'un salarié"),
         ('card', "Carte bancaire"),
         ('manual', "Saisie manuelle")],
        string="Origine de la dépense", default='manual', copy=False, tracking=True,
    )
    expense_merchant_id = fields.Many2one(
        'res.partner', string="Fiche du commerçant", copy=False, domain=[('is_company', '=', True)],
        help="Commerçant qui a facturé, quand il est connu (facultatif, jamais créé automatiquement). Sur un "
             "reçu de salarié, le tiers comptable est le salarié remboursé : le commerçant reste ici.",
    )
    expense_category_id = fields.Many2one(
        'product.product', string="Catégorie de dépense", compute='_compute_expense_category_id',
        inverse='_inverse_expense_category_id', readonly=False, domain=[('type', '=', 'service'), ('purchase_ok', '=', True)],
        help="Catégorie commune à toutes les lignes de la dépense : c'est le produit des lignes, qui donne le compte de "
             "charge. Elle se saisit une fois ; une ligne peut exceptionnellement en avoir une autre.")
    expense_show_lines = fields.Boolean(
        compute='_compute_expense_show_lines',
        help="Le salarié ne voit pas les lignes de sa dépense : l'OCR les remplit avec leurs taux. Elles ne lui sont "
             "montrées que dans les cas exceptionnels : saisie à la main (pas de lecture du justificatif), ou ligne "
             "à laquelle il manque encore une taxe ou une catégorie.")
    expense_project_id = fields.Many2one(
        'project.project', string="Projet de la dépense", compute='_compute_expense_project_id',
        inverse='_inverse_expense_project_id', readonly=False,
        help="Imputation de toute la pièce sur un projet : distribution analytique à 100 % sur son compte analytique.",
    )
    allowed_project_ids = fields.Many2many(
        'project.project', string="Projets autorisés", compute='_compute_allowed_project_ids',
        help="Projets sur lesquels le salarié peut imputer ses frais : ceux où il est staffé à la date de la dépense "
             "et ceux dont il est directeur de mission.")
    ocr_result_ids = fields.One2many('expense.ocr.result', 'move_id', string="Lectures OCR", readonly=True)
    expense_warning = fields.Text(
        compute='_compute_expense_warning',
        help="Ce qu'il reste à compléter sur la dépense et les remarques de la lecture du justificatif, en bandeau.")
    approval_director_ids = fields.Many2many(
        'res.users', string="Directeurs de mission valideurs",
        compute='_compute_approval_director_ids', search='_search_approval_director_ids',
    )

    @api.depends('ocr_result_ids.warnings', 'ocr_result_ids.state', 'ocr_result_ids.payload',
                 'invoice_line_ids.tax_ids', 'invoice_line_ids.product_id', 'invoice_line_ids.price_unit',
                 'amount_total', 'amount_tax', 'state', 'expense_origin')
    def _compute_expense_warning(self):
        """Bandeau d'une dépense en brouillon, recalculé à mesure que le salarié la complète : catégorie et taux de
        TVA de chaque ligne, puis remarques de la lecture du justificatif et écarts avec le ticket."""
        for move in self:
            messages = []
            if move.state == 'draft' and move.expense_origin in EXPENSE_ORIGINS:
                done = move.ocr_result_ids.filtered(lambda r: r.state == 'done')[:1]
                if done and done.warnings:
                    messages.extend(done.warnings.split('\n'))
                filled = move.invoice_line_ids.filtered(lambda l: l.display_type == 'product' and l.price_unit)
                missing_category = bool(filled.filtered(lambda l: not l.product_id))
                missing_tax = bool(filled.filtered(lambda l: not l.tax_ids))
                if missing_category:
                    messages.append(
                        _("Choisissez la catégorie de dépense.") if not move.expense_category_id and not any(
                            filled.mapped('product_id')) else _("Une ligne n'a pas de catégorie de dépense."))
                if missing_tax:
                    messages.append(_("Choisissez le taux de TVA de chaque ligne."))
                if done and filled and not missing_tax:
                    messages.extend(move._ocr_compare_with_ticket(json.loads(done.payload or '{}')))
            move.expense_warning = '\n'.join(messages) or False

    def _ocr_compare_with_ticket(self, payload):
        """Écarts entre la pièce et le ticket lu : le total, puis la TVA (arrondi à la ligne ou en pied de ticket)."""
        self.ensure_one()
        messages = []
        total, tax_total = payload.get('total'), payload.get('tax_total')
        if total is not None and abs(self.amount_total - total) > 0.005:
            messages.append(_("Le total de la pièce (%(piece)s) diffère de celui du ticket (%(ticket)s).",
                              piece=self.amount_total, ticket=total))
        if tax_total is not None and abs(self.amount_tax - tax_total) > 0.005:
            messages.append(_(
                "La TVA de la pièce (%(piece)s) diffère de celle du ticket (%(ticket)s) : écart d'arrondi "
                "possible, selon que le commerçant calcule la TVA à la ligne ou en pied de ticket.",
                piece=self.amount_tax, ticket=tax_total))
        return messages

    def _get_approval_directors(self):
        """Directeurs de mission des projets de la pièce (hook surchargeable, utilisé par les tests)."""
        self.ensure_one()
        return self.line_ids.rel_project_ids.mapped('user_id')

    @api.depends('line_ids.rel_project_ids')
    def _compute_approval_director_ids(self):
        for move in self:
            move.approval_director_ids = move._get_approval_directors() if move.id else False

    def _search_approval_director_ids(self, operator, value):
        """Permet de rechercher les pièces d'un directeur de mission (filtre « À valider (DM) »).

        rel_project_ids n'est pas stocké sur la pièce : on passe par celui des lignes, qui l'est.
        """
        return [('line_ids.rel_project_ids.user_id', operator, value)]

    def _check_allow_write_under_validation(self, vals):
        """Une fois les deux niveaux validés, la comptabilisation écrit elle-même sur la pièce encore en
        brouillon (numéro, contrôle, etc.) avant de la passer à « posted » : ces écritures sont permises.
        Pendant la validation, modifier la pièce reste interdit (montants, lignes, fournisseur)."""
        if self.validation_status == 'validated':
            return True
        return super()._check_allow_write_under_validation(vals)

    # ------------------------------------------------------------------
    # Paiement : mode de paiement des dépenses
    # ------------------------------------------------------------------
    def _compute_preferred_payment_method_line_id(self):
        super()._compute_preferred_payment_method_line_id()
        self._apply_expense_payment_method()

    def _apply_expense_payment_method(self):
        """Mode de paiement des dépenses de la société, quand le fournisseur n'en a pas de préféré."""
        for move in self.filtered(
                lambda m: m.move_type in APPROVAL_MOVE_TYPES and not m.preferred_payment_method_line_id):
            move.preferred_payment_method_line_id = move.company_id.expense_payment_method_line_id

    def create_account_payment_line(self):
        # Les pièces créées avant le réglage n'ont pas de mode préféré : on applique le défaut au moment de payer.
        self.filtered(lambda m: m.state == 'posted')._apply_expense_payment_method()
        return super().create_account_payment_line()

    # ------------------------------------------------------------------
    # Reçu d'un salarié : employé, projet du staffing, IBAN
    # ------------------------------------------------------------------
    def _get_expense_employee(self):
        """Salarié remboursé par la pièce : l'employé dont le contact de travail est le tiers de la pièce.

        Rien n'est stocké sur la pièce : le lien se lit depuis le tiers. Une carte (lot 3) aura pour tiers le
        commerçant, son porteur viendra de la carte et non de ce calcul.
        """
        self.ensure_one()
        return self.sudo().partner_id.employee_ids.filtered(
            lambda e: e.company_id == self.company_id)[:1]

    @api.model_create_multi
    def create(self, vals_list):
        # Un reçu créé sans tiers est celui de l'employé de l'utilisateur : tiers et IBAN viennent de sa fiche.
        for vals in vals_list:
            if vals.get('move_type') == 'in_invoice' and vals.get('expense_origin') == 'employee_receipt' \
                    and not vals.get('partner_id'):
                employee = self.env['hr.employee'].sudo().search([
                    ('user_id', '=', self.env.uid),
                    ('company_id', '=', vals.get('company_id') or self.env.company.id)], limit=1)
                if employee.work_contact_id:
                    vals['partner_id'] = employee.work_contact_id.id
                    if employee.bank_account_id:
                        vals.setdefault('partner_bank_id', employee.bank_account_id.id)
        return super().create(vals_list)

    @api.depends('invoice_line_ids.product_id', 'invoice_line_ids.display_type')
    def _compute_expense_category_id(self):
        for move in self:
            lines = move.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
            products = lines.mapped('product_id')
            common = len(products) == 1 and not lines.filtered(lambda l: not l.product_id)
            move.expense_category_id = products if common else False

    def _inverse_expense_category_id(self):
        for move in self:
            product = move.expense_category_id
            if not product:
                continue
            old_position = move.fiscal_position_id
            new_position = move._expense_fiscal_position(product)
            if new_position != old_position:
                move.fiscal_position_id = new_position
            for line in move.invoice_line_ids.filtered(lambda l: l.display_type == 'product' and l.product_id != product):
                price, name, discount, current = line.price_unit, line.name, line.discount, line.tax_ids
                line.write({'product_id': product.id})
                # Changer le produit remet le prix de revient du produit (souvent 0) et sa taxe par défaut : le montant
                # saisi ou lu sur le ticket est rétabli. Le taux de chaque ligne est conservé, et c'est la position
                # fiscale de la catégorie qui donne le régime de TVA (déductible, carburant, non déductible...).
                taxes = new_position.map_tax(move._expense_standard_taxes(current, old_position)) if current else line.tax_ids
                line.write({
                    'price_unit': price, 'name': name, 'discount': discount,
                    'tax_ids': [Command.set(taxes.ids)],
                })

    @api.depends('ocr_result_ids.state', 'invoice_line_ids.tax_ids', 'invoice_line_ids.product_id',
                 'invoice_line_ids.price_unit')
    def _compute_expense_show_lines(self):
        for move in self:
            done = move.ocr_result_ids.filtered(lambda r: r.state == 'done')
            filled = move.invoice_line_ids.filtered(lambda l: l.display_type == 'product' and l.price_unit)
            incomplete = filled.filtered(lambda l: not l.tax_ids or not l.product_id)
            move.expense_show_lines = not done or bool(incomplete)

    @api.depends('line_ids.analytic_distribution')
    def _compute_expense_project_id(self):
        for move in self:
            projects = move.line_ids.rel_project_ids
            move.expense_project_id = projects if len(projects) == 1 else False

    def _inverse_expense_project_id(self):
        for move in self:
            project = move.expense_project_id
            if project:
                move._ensure_expense_outsourcing_link(project)
            distribution = {str(project.account_id.id): 100} if project.account_id else False
            move.invoice_line_ids.write({'analytic_distribution': distribution})

    def _ensure_expense_outsourcing_link(self, project):
        """Déclare le tiers de la pièce comme fournisseur du projet (type « Autres ») s'il ne l'est pas déjà.

        project_accounting refuse toute écriture liée à un projet dont le tiers n'est ni le client ni un fournisseur
        de l'onglet Achats. Le lien est créé au choix du projet : pour un reçu, c'est le salarié remboursé ; pour une
        dépense par carte, c'est le vrai fournisseur (le commerçant), pas le porteur de la carte. Il est créé avec les
        droits système, mais seulement pour un projet que celui qui dépense a le droit de choisir (staffé ou
        directeur de mission), ou pour l'Office : sinon la contrainte reste en vigueur.
        """
        self.ensure_one()
        partner = self._get_expense_link_partner()
        if not partner:
            return
        if project not in self.allowed_project_ids and not self.env.user.has_group(OFFICE_GROUP):
            return
        Link = self.env['project.outsourcing.link'].sudo()
        if not Link.search_count([('project_id', '=', project.id), ('partner_id', '=', partner.id)]):
            Link.create({
                'project_id': project.id, 'partner_id': partner.id, 'link_type': 'other',
                'company_id': project.company_id.id or self.company_id.id,
            })

    def _get_expense_link_partner(self):
        """Tiers à déclarer comme fournisseur du projet : le contact du salarié (reçu) ou le commerçant (carte)."""
        self.ensure_one()
        if self.expense_origin == 'card':
            return self.partner_id
        employee = self._get_expense_employee()
        return employee.work_contact_id if employee else self.env['res.partner']

    def _get_expense_spender(self):
        """Salarié qui dépense, dont le staffing limite les projets : l'employé remboursé (reçu) ou, pour une carte,
        l'employé de l'utilisateur connecté (le porteur de la carte viendra de qonto.card au lot 3)."""
        self.ensure_one()
        if self.expense_origin == 'card':
            return self.env['hr.employee'].sudo().search([
                ('user_id', '=', self.env.uid), ('company_id', '=', self.company_id.id)], limit=1)
        if self.expense_origin == 'employee_receipt':
            return self._get_expense_employee()
        return self.env['hr.employee']

    @api.depends('partner_id', 'invoice_date', 'date')
    def _compute_allowed_project_ids(self):
        Need = self.env['staffing.need'].sudo()
        Project = self.env['project.project'].sudo()
        for move in self:
            employee = move._get_expense_spender() if move.id else False
            if not employee:
                move.allowed_project_ids = False
                continue
            day = move.invoice_date or move.date or fields.Date.context_today(move)
            needs = Need.search([
                ('staffed_employee_id', '=', employee.id),
                ('begin_date', '<=', day),
                '|', ('end_date', '=', False), ('end_date', '>=', day),
            ])
            directed = Project.search([('user_id', '=', employee.user_id.id)]) if employee.user_id else Project
            move.allowed_project_ids = (needs.project_id | directed).filtered(
                lambda project: not project.company_id or project.company_id == move.company_id)

    def _check_expense_staffing(self):
        """Un salarié n'impute ses frais qu'aux projets sur lesquels il est staffé à la date de la dépense, ou dont il
        est le directeur de mission.

        L'Office (TAZ-ADV) peut corriger l'imputation d'une pièce sans cette limite.
        """
        if self.env.user.has_group(OFFICE_GROUP):
            return
        for move in self.filtered(lambda m: m.expense_origin in EXPENSE_ORIGINS and m._get_expense_spender()):
            outside = move.line_ids.rel_project_ids - move.allowed_project_ids
            if outside:
                raise ValidationError(_(
                    "%(employee)s n'est ni staffé le %(date)s ni directeur de mission sur : %(projects)s.",
                    employee=move._get_expense_spender().name,
                    date=move.invoice_date or move.date,
                    projects=', '.join(outside.mapped('display_name'))))

    def _check_expense_lines(self):
        """Une dépense ne part en validation que si elle a sa catégorie (le produit des lignes, qui donne le compte de
        charge) et si chaque ligne a sa taxe (une taxe à 0 % pour une ligne sans TVA)."""
        for move in self.filtered(lambda m: m.expense_origin in EXPENSE_ORIGINS):
            lines = move.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
            if lines.filtered(lambda l: not l.product_id):
                raise ValidationError(_(
                    "Choisissez la catégorie de dépense de %s avant de l'envoyer en validation.",
                    move.display_name))
            if lines.filtered(lambda l: not l.tax_ids):
                raise ValidationError(_(
                    "Choisissez le taux de TVA de chaque ligne de %s avant de l'envoyer en validation.",
                    move.display_name))

    def request_validation(self):
        self._check_expense_staffing()
        self._check_expense_lines()
        return super().request_validation()

    @api.model
    def _cron_create_reimbursement_order(self):
        """Tâche hebdomadaire : ajoute les reçus de salariés approuvés à l'ordre de paiement en brouillon.

        Un reçu sans IBAN de confiance du salarié est laissé de côté : il reste à traiter à la main.
        Retourne les reçus ajoutés, pour les tests.
        """
        added = self.browse()
        for company in self.env['res.company'].search([('expense_reimbursement_method_line_id', '!=', False)]):
            receipts = self.with_company(company).search([
                ('company_id', '=', company.id), ('move_type', '=', 'in_invoice'),
                ('expense_origin', '=', 'employee_receipt'),
                ('partner_id.employee_ids', '!=', False), ('state', '=', 'posted'),
                ('payment_state', 'in', ('not_paid', 'partial')),
            ]).filtered(lambda m: not m.payment_line_count and m.partner_bank_id.allow_out_payment)
            if not receipts:
                continue
            receipts.write({'preferred_payment_method_line_id': company.expense_reimbursement_method_line_id.id})
            receipts.create_account_payment_line()
            added |= receipts
        return added

    # ------------------------------------------------------------------
    # Lecture des justificatifs (OCR)
    # ------------------------------------------------------------------
    def action_run_ocr(self):
        """Lit les justificatifs de la pièce qui ne l'ont pas encore été (une lecture par fichier)."""
        Result = self.env['expense.ocr.result']
        results = Result
        if not self.env['expense.ocr'].is_configured():
            return True
        for move in self.filtered(lambda m: m.state == 'draft'):
            attachments = self.env['ir.attachment'].sudo().search([
                ('res_model', '=', 'account.move'), ('res_id', '=', move.id)]).filtered(Result._is_readable)
            known = set(move.ocr_result_ids.mapped('checksum'))
            for attachment in attachments:
                if attachment.checksum and attachment.checksum not in known:
                    known.add(attachment.checksum)
                    results |= Result.sudo().create({
                        'move_id': move.id, 'attachment_id': attachment.id, 'checksum': attachment.checksum})
        results.sudo(False).action_read()
        return True

    @api.model
    def _ocr_find_tax(self, company, rate):
        """Taxe d'achat « standard » (déductible) active de la société au taux lu ; la taxe « TTC » est préférée, car
        elle part du total du ticket sans arrondi à reconstituer.

        Les taxes qui ne sont que la destination d'une position fiscale de frais (carburant, non déductible...) sont
        écartées : c'est la position fiscale de la dépense qui les applique ensuite, au même taux.
        """
        if rate is None:
            return self.env['account.tax']
        taxes = self.env['account.tax'].search([
            ('company_id', '=', company.id), ('type_tax_use', '=', 'purchase'), ('amount_type', '=', 'percent'),
            ('amount', '=', rate), ('active', '=', True)])
        positions = self.env['product.template'].sudo().with_company(company).search(
            [('expense_fiscal_position_id', '!=', False)]).mapped('expense_fiscal_position_id')
        taxes = (taxes - positions.tax_ids.tax_dest_id) or taxes
        # price_include est calculé (non stocké) : le tri se fait en Python, taxe « TTC » d'abord.
        return taxes.sorted(lambda t: (not t.price_include, t.sequence, t.id))[:1]

    def _expense_fiscal_position(self, product):
        """Position fiscale d'une dépense de cette catégorie : celle de la catégorie, sinon celle du tiers."""
        self.ensure_one()
        return product.expense_fiscal_position_id or self.env['account.fiscal.position'].with_company(
            self.company_id)._get_fiscal_position(self.partner_id)

    @staticmethod
    def _expense_standard_taxes(taxes, position):
        """Taxes de départ de taxes de ligne : celles qu'une position fiscale de frais a remplacées, sinon elles-mêmes."""
        result = taxes.browse()
        for tax in taxes:
            source = position.tax_ids.filtered(lambda m: m.tax_dest_id == tax).tax_src_id if position else False
            result |= source[:1] if source else tax
        return result

    def _ocr_fill_lines(self, data, template):
        """Une ligne de reçu par ligne du ticket, avec sa taxe. Renvoie les remarques à signaler."""
        notes = []
        total = data['total']
        merchant = (data.get('merchant') or '').strip()
        ticket_lines = [dict(line) for line in data.get('lines') or [] if line.get('amount') is not None]
        details = [d for d in data.get('tax_details') or [] if d.get('rate') is not None]
        by_rate = None
        if len(details) > 1 and all(d.get('net') is not None and d.get('tax') is not None for d in details):
            # Plusieurs taux au pied du ticket : une ligne par taux, au montant TTC du récapitulatif.
            grouped = [{
                'name': _("%(merchant)s - TVA %(rate)s %%", merchant=merchant, rate=f"{d['rate']:g}"),
                'amount': round(d['net'] + d['tax'], 2), 'tax_rate': d['rate'],
            } for d in details]
            if abs(sum(line['amount'] for line in grouped) - total) <= 0.01:
                by_rate = grouped
        if by_rate:
            ticket_lines = by_rate
        else:
            if ticket_lines and abs(sum(line['amount'] for line in ticket_lines) - total) > 0.005:
                notes.append(_("les lignes lues ne totalisent pas le total du ticket : une seule ligne a été créée"))
                ticket_lines = []
            if not ticket_lines:
                ticket_lines = [{'name': merchant, 'amount': total, 'tax_rate': None}]
            if len(details) == 1:
                # Un seul taux au pied du ticket : c'est celui de toutes les lignes (taux lu, pas déduit).
                for line in ticket_lines:
                    if line.get('tax_rate') is None:
                        line['tax_rate'] = details[0]['rate']
        missing = set()
        values = []
        for line in ticket_lines:
            rate = line.get('tax_rate')
            found = self._ocr_find_tax(self.company_id, rate)
            if rate is not None and not found:
                missing.add(rate)
            tax = self.fiscal_position_id.map_tax(found)
            price = line['amount']
            if tax and not tax.price_include:
                price = line['amount'] / (1 + tax.amount / 100)
            values.append({
                'name': line.get('name') or merchant or template.name,
                'quantity': 1, 'price_unit': price, 'tax_ids': [Command.set(tax.ids)],
            })
        if missing:
            notes.append(_(
                "votre ticket comporte une taxe à %s %%, qui n'existe pas ou n'est pas activée : contactez le "
                "gestionnaire des notes de frais pour l'activer",
                ', '.join(str(r) for r in sorted(missing))))
        first, others = values[0], values[1:]
        template.write(first)
        for vals in others:
            self.write({'invoice_line_ids': [Command.create(dict(
                vals, product_id=template.product_id.id, analytic_distribution=template.analytic_distribution))]})
        return notes

    def _apply_ocr_result(self, data):
        """Pré-remplit la pièce avec la lecture : jamais par-dessus une valeur déjà saisie.

        Le taux de TVA d'une ligne n'est posé que s'il est lu sur le ticket ; sinon le salarié le choisit ligne par
        ligne (bandeau). Le salarié vérifie toujours : le message du chatter le rappelle, et le bandeau signale en
        direct les écarts de total ou de TVA avec le ticket (certains commerçants calculent la TVA à la ligne,
        d'autres en pied de ticket).
        """
        self.ensure_one()
        if self.state != 'draft':
            return []
        if data.get('date') and not self.invoice_date:
            self.invoice_date = data['date']
        merchant = (data.get('merchant') or '').strip()
        if merchant and not self.expense_merchant_id:
            partners = self.env['res.partner'].search(
                [('is_company', '=', True), ('name', '=ilike', merchant)], limit=2)
            if len(partners) == 1:
                self.expense_merchant_id = partners
        total = data.get('total')
        notes = []
        template = self.invoice_line_ids[:1]
        if total and len(self.invoice_line_ids) == 1 and not template.price_unit and not template.tax_ids:
            notes = self._ocr_fill_lines(data, template)
        elif total:
            notes.append(_("la pièce contenait déjà des lignes : elles n'ont pas été modifiées"))
        parts = []
        if merchant:
            parts.append(_("commerçant %s", merchant))
        if data.get('date'):
            parts.append(_("date %s", data['date']))
        if total is not None:
            parts.append(_("total %(total)s %(currency)s TTC", total=total, currency=data.get('currency') or ''))
        if data.get('tax_total') is not None:
            parts.append(_("dont TVA %s", data['tax_total']))
        body = _("Lecture du justificatif : ") + ', '.join(parts) + _(
            " (confiance %.0f %%).", data.get('confidence', 0.0) * 100)
        if notes:
            first = notes[0][:1].upper() + notes[0][1:]
            body += ' ' + ' ; '.join([first] + notes[1:]) + '.'
        body += _(" À vérifier par le salarié avant l'envoi en validation.")
        self.message_post(body=body)
        return notes

    @api.model
    def _cron_run_ocr(self, limit=50):
        """Tâche planifiée : lit les justificatifs ajoutés aux reçus en brouillon, si un service OCR est configuré."""
        if not self.env['expense.ocr'].is_configured():
            return self.browse()
        moves = self.search([
            ('move_type', '=', 'in_invoice'), ('expense_origin', 'in', list(EXPENSE_ORIGINS)), ('state', '=', 'draft'),
            ('create_date', '>=', fields.Datetime.subtract(fields.Datetime.now(), days=7)),
        ], limit=limit, order='id desc')
        moves.action_run_ocr()
        return moves

    # ------------------------------------------------------------------
    # Rapprochement avec un bon de commande (comme le ferait l'Office)
    # ------------------------------------------------------------------
    def action_post(self):
        res = super().action_post()
        self.filtered(lambda m: m.expense_origin in EXPENSE_ORIGINS and m.state == 'posted')._reconcile_with_purchase_order()
        return res

    def _reconcile_with_purchase_order(self):
        """À la comptabilisation, rapproche la dépense du bon de commande de son tiers sur le projet, en appelant le
        wizard « Rapprochement BC » exactement comme l'Office le ferait à la main. Le tiers est celui de la pièce : le
        salarié pour une note de frais, le vrai fournisseur (le commerçant) pour une dépense par carte.

        - le BC du tiers pour le projet est unique et cumulatif : il reçoit une ligne par ligne de la dépense ;
        - s'il n'existe pas, il est créé et confirmé ;
        - l'enveloppe « frais divers » du projet est consommée si elle existe.

        Une dépense sans projet, ou qui touche plusieurs projets, n'est pas rapprochée. Un échec du rapprochement ne
        bloque jamais la comptabilisation : il est noté dans le chatter, l'Office reprend à la main.
        """
        Wizard = self.env['bill.po.reconcile.wizard'].sudo()
        for move in self:
            projects = move.line_ids.rel_project_ids
            lines = move.invoice_line_ids.filtered(lambda l: l.display_type == 'product' and not l.purchase_line_id)
            if len(projects) != 1 or not lines or not move.partner_id:
                continue
            try:
                with self.env.cr.savepoint():
                    wizard = Wizard.with_company(move.company_id).create(
                        {'move_id': move.id, 'project_ids': [Command.set(projects.ids)]})
                    wizard._onchange_project_ids()  # BC du salarié sur le projet, enveloppe présélectionnée
                    if wizard.order_ids:
                        wizard.append_order_id = wizard.order_ids.sorted('id')[:1]
                    else:
                        wizard.action_new_order()
                    wizard.action_associate()
                    move.invoice_line_ids.purchase_line_id.sudo().filtered(
                        lambda pol: not pol.expense_move_id).write({'expense_move_id': move.id})
            except (UserError, ValidationError) as error:
                _logger.warning("Rapprochement BC impossible pour la dépense %s : %s", move.id, error)
                move.message_post(body=_("Rapprochement avec un bon de commande impossible : %s", error))

    def button_cancel(self):
        res = super().button_cancel()
        self._remove_expense_order_lines()
        return res

    def _remove_expense_order_lines(self):
        """Retour arrière du rapprochement : les lignes de BC créées pour la dépense sont retirées, et le BC est annulé
        s'il n'a plus de ligne. L'enveloppe consommée est déjà remise par le module du rapprochement."""
        pols = self.env['purchase.order.line'].sudo().search([('expense_move_id', 'in', self.ids)])
        if not pols:
            return
        orders = pols.order_id
        # Les lignes de la dépense (annulée) perdent leur ligne de BC par la clé étrangère (ondelete « set null »).
        pols.with_context(bill_po_reconcile_replace=True).unlink()
        self.env.invalidate_all()
        for order in orders:
            if not order.order_line.filtered(lambda l: not l.display_type):
                order.button_cancel()
