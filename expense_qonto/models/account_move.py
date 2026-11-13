import logging
from datetime import timedelta

from odoo import Command, _, api, fields, models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    qonto_transaction_id = fields.Char(string="Transaction Qonto", copy=False, index=True, readonly=True)
    expense_card_id = fields.Many2one('account.bank.statement.card', string="Carte", copy=False, readonly=True)
    qonto_transaction_date = fields.Date(string="Date du paiement par carte", copy=False, readonly=True)

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
        if tx.get('operation_type') != 'card' or tx.get('side') != 'debit' or not tx.get('card_id') \
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
        move = self.sudo().with_company(company).with_context(tracking_disable=False).create({
            'move_type': 'in_invoice', 'expense_origin': 'card', 'company_id': company.id, 'partner_id': partner.id,
            'invoice_date': settled, 'date': settled, 'currency_id': currency.id, 'journal_id': self._qonto_purchase_journal(company).id,
            'qonto_transaction_id': transaction_id, 'expense_card_id': card.id, 'qonto_transaction_date': settled,
            'ref': tx.get('reference') or transaction_id,
            'invoice_line_ids': [Command.create({'name': label, 'quantity': 1, 'price_unit': amount})],
        })
        message = _("Dépense créée depuis la transaction par carte Qonto « %(label)s » de %(amount).2f %(currency)s.",
                    label=label, amount=amount, currency=currency.name)
        if not spender:
            message += " " + _("La carte n'a pas de porteur : l'Office doit la rattacher à un salarié.")
        move.message_post(body=message)
        if spender.user_id:
            move.sudo().activity_schedule(
                'mail.mail_activity_data_todo', user_id=spender.user_id.id,
                summary=_("Compléter la dépense par carte : justificatif et projet"))
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
