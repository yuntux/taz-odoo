import logging
from datetime import timedelta

from odoo import Command, _, api, fields, models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    qonto_transaction_id = fields.Char(string="Transaction Qonto", copy=False, index=True, readonly=True)
    expense_card_id = fields.Many2one('account.bank.statement.card', string="Carte", copy=False, readonly=True)
    qonto_transaction_date = fields.Date(string="Date du paiement par carte", copy=False, readonly=True)
    qonto_reminder_stage = fields.Integer(
        string="Relances du justificatif", copy=False, readonly=True,
        help="0 : aucune relance ; 1 : le porteur a été relancé ; 2 : le directeur de mission l'a été aussi.")
    qonto_reminder_date = fields.Date(string="Dernière relance", copy=False, readonly=True)

    _sql_constraints = [
        ('qonto_transaction_id_unique', 'unique(qonto_transaction_id, company_id)',
         "Cette transaction Qonto a déjà créé une dépense."),
    ]

    def _get_expense_spender(self):
        self.ensure_one()
        if self.expense_origin == 'card' and self.expense_card_id:
            return self.expense_card_id.employee_id
        return super()._get_expense_spender()

    # ------------------------------------------------------------------
    # Création depuis une transaction Qonto
    # ------------------------------------------------------------------
    @api.model
    def _qonto_card_for(self, company, tx):
        """Carte de la transaction, créée si elle est inconnue (sans porteur : l'Office la rattache)."""
        Card = self.env['account.bank.statement.card'].sudo()
        card = Card.with_context(active_test=False).search(
            [('company_id', '=', company.id), ('qonto_card_id', '=', tx['card_id'])], limit=1)
        if card:
            return card
        journal = self.env['account.journal'].sudo().search([
            ('company_id', '=', company.id),
            ('statement_import_api_account_identifier', '=', tx.get('bank_account_id'))], limit=1)
        if not journal:
            return Card
        return Card.create({
            'code': tx.get('card_last_digits') or tx['card_id'][-4:], 'qonto_card_id': tx['card_id'],
            'company_id': company.id, 'journal_id': journal.id,
            'name': _("Carte %s", tx.get('card_last_digits') or tx['card_id'][-4:])})

    @api.model
    def _qonto_find_partner(self, company, label):
        domain = [('name', '=ilike', label), ('is_company', '=', True), '|', ('company_id', '=', False),
                  ('company_id', '=', company.id)]
        return self.env['res.partner'].sudo().search(domain, limit=1) or company.sudo().qonto_default_partner_id

    @api.model
    def _qonto_create_expense(self, company, tx):
        """Crée la dépense d'une transaction par carte réglée ; retourne la dépense ou un recordset vide.

        Idempotent : une transaction reçue plusieurs fois (webhook rejoué, rattrapage) ne crée qu'une dépense.
        """
        if tx.get('operation_type') != 'card' or tx.get('side') not in ('debit', 'credit') or not tx.get('card_id') \
                or tx.get('status') != 'completed':
            return self.browse()
        transaction_id = tx.get('transaction_id') or tx.get('id')
        move = self.sudo().search([('qonto_transaction_id', '=', transaction_id), ('company_id', '=', company.id)], limit=1)
        if move:
            return move
        card = self._qonto_card_for(company, tx)
        if not card:
            _logger.warning("Qonto : compte %s inconnu, transaction %s ignorée.", tx.get('bank_account_id'), transaction_id)
            return self.browse()
        label = tx.get('label') or _("Carte")
        partner = self._qonto_find_partner(company, label)
        if not partner:
            _logger.warning("Qonto : aucun tiers pour « %s » et pas de tiers par défaut, transaction %s ignorée.", label, transaction_id)
            return self.browse()
        settled = (tx.get('settled_at') or tx.get('emitted_at') or '')[:10] or fields.Date.today()
        amount = abs(float(tx.get('amount') or 0.0))
        currency = self.env['res.currency'].search([('name', '=', tx.get('currency'))], limit=1) or company.currency_id
        spender = card.employee_id
        refund = tx['side'] == 'credit'
        move = self.sudo().with_company(company).with_context(tracking_disable=False).create({
            'move_type': 'in_refund' if refund else 'in_invoice', 'expense_origin': 'card', 'company_id': company.id, 'partner_id': partner.id,
            'invoice_date': settled, 'date': settled, 'currency_id': currency.id, 'journal_id': self._qonto_purchase_journal(company).id,
            'qonto_transaction_id': transaction_id, 'expense_card_id': card.id, 'qonto_transaction_date': settled,
            'ref': tx.get('reference') or transaction_id,
            'invoice_line_ids': [Command.create({'name': label, 'quantity': 1, 'price_unit': amount})],
        })
        message = (_("Avoir créé depuis le remboursement par carte Qonto « %(label)s » de %(amount).2f %(currency)s : "
                     "l'Office le rattache à la dépense d'origine.", label=label, amount=amount, currency=currency.name)
                   if refund else
                   _("Dépense créée depuis la transaction par carte Qonto « %(label)s » de %(amount).2f %(currency)s.",
                     label=label, amount=amount, currency=currency.name))
        if not spender:
            message += " " + _("La carte n'a pas de porteur : l'Office doit la rattacher à un salarié.")
        move.message_post(body=message)
        if spender.user_id and not refund:
            move.sudo().activity_schedule(
                'mail.mail_activity_data_todo', user_id=spender.user_id.id,
                summary=_("Compléter la dépense par carte : justificatif et projet"))
            # Notification adressée au porteur par le robot Odoo (l'auteur n'est jamais notifié) : e-mail, ou push de
            # la PWA s'il l'a installée.
            move._expense_notify(
                spender.user_id.partner_id, _("Dépense par carte à compléter"),
                _("Votre carte a payé %(amount).2f %(currency)s chez %(label)s : ajoutez le justificatif et choisissez "
                  "le projet sur %(move)s.", amount=amount, currency=currency.name, label=label, move=move.display_name))
        elif refund:
            move._expense_notify(
                move._expense_management_partners(), _("Remboursement par carte à rattacher"), message)
        return move

    @api.model
    def _qonto_purchase_journal(self, company):
        return self.env['account.journal'].sudo().search(
            [('company_id', '=', company.id), ('type', '=', 'purchase')], limit=1)

    # ------------------------------------------------------------------
    # Paiement automatique : la carte a déjà payé
    # ------------------------------------------------------------------
    def action_post(self):
        res = super().action_post()
        self.filtered(lambda m: m.state == 'posted' and m.qonto_transaction_id)._qonto_register_payment()
        return res

    def _qonto_register_payment(self):
        for move in self.filtered(lambda m: m.payment_state in ('not_paid', 'partial') and m.expense_card_id):
            wizard = self.env['account.payment.register'].with_context(
                active_model='account.move', active_ids=move.ids).create({
                    'journal_id': move.expense_card_id.journal_id.id,
                    'payment_date': move.qonto_transaction_date or fields.Date.today(),
                    'communication': move.ref,
                })
            wizard._create_payments()

    # ------------------------------------------------------------------
    # Lecture planifiée, filet du webhook
    # ------------------------------------------------------------------
    @api.model
    def _cron_qonto_import(self):
        """Relit les transactions par carte modifiées depuis la dernière lecture réussie (moins une heure)."""
        created = self.browse()
        journals = self.env['account.journal'].sudo().search([('statement_import_api_account_identifier', '!=', False)])
        for journal in journals:
            company = journal.company_id
            start = (journal.statement_import_api_last_success or fields.Datetime.now() - timedelta(days=7)) - timedelta(hours=1)
            now = fields.Datetime.now()
            try:
                transactions = self.env['qonto.api'].list_transactions(
                    company, journal.statement_import_api_account_identifier, start)
            except Exception:
                _logger.exception("Qonto : lecture des transactions du journal %s impossible.", journal.display_name)
                continue
            for tx in transactions:
                created |= self._qonto_create_expense(company, tx)
            journal.statement_import_api_last_success = now
        return created

    # ------------------------------------------------------------------
    # Relances d'un justificatif manquant
    # ------------------------------------------------------------------
    def _qonto_has_receipt(self):
        self.ensure_one()
        return bool(self.env['ir.attachment'].sudo().search_count(
            [('res_model', '=', 'account.move'), ('res_id', '=', self.id)]))

    @api.model
    def _cron_qonto_reminders(self):
        """Rappelle le porteur d'une dépense par carte sans justificatif, puis le directeur de mission (ou la
        direction) après le même délai. Une seule relance par étape. Retourne les dépenses relancées."""
        reminded = self.browse()
        today = fields.Date.context_today(self)
        for company in self.env['res.company'].search([('qonto_reminder_days', '>', 0)]):
            limit = today - timedelta(days=company.qonto_reminder_days)
            moves = self.sudo().search([
                ('company_id', '=', company.id), ('move_type', '=', 'in_invoice'), ('expense_origin', '=', 'card'),
                ('state', '=', 'draft'), ('qonto_transaction_id', '!=', False), ('qonto_reminder_stage', '<', 2),
                ('qonto_transaction_date', '<=', limit)])
            for move in moves:
                last = move.qonto_reminder_date or move.qonto_transaction_date
                if last > limit or move._qonto_has_receipt():
                    continue
                if move.qonto_reminder_stage == 0:
                    spender = move._get_expense_spender()
                    partners = spender.user_id.partner_id
                    subject = _("Justificatif manquant")
                    body = _("Il manque le justificatif de %(move)s (paiement du %(date)s).",
                             move=move.display_name, date=move.qonto_transaction_date)
                else:
                    partners = move.approval_director_ids.partner_id or move._expense_management_partners()
                    subject = _("Justificatif manquant à relancer")
                    body = _("Le justificatif de %(move)s manque toujours, malgré la relance du porteur.",
                             move=move.display_name)
                move._expense_notify(partners, subject, body)
                move.write({'qonto_reminder_stage': move.qonto_reminder_stage + 1, 'qonto_reminder_date': today})
                reminded |= move
        return reminded
