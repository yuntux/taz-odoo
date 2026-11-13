import logging

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

BASE_URL = "https://thirdparty.qonto.com/v2/"
TIMEOUT = 20


class QontoApi(models.AbstractModel):
    _name = 'qonto.api'
    _description = "Client de l'API Qonto"

    @api.model
    def _call(self, company, method, path, params=None, payload=None, ok=(200,)):
        """Appel authentifié par la clé d'API de la société (identifiant:secret) ; retourne le JSON, ou {} sans corps."""
        company = company.sudo()
        if not (company.statement_import_api_login and company.statement_import_api_password):
            raise UserError(_("Les identifiants de l'API Qonto ne sont pas renseignés sur la société %s.", company.name))
        # Bac à sable Qonto : paramètres système expense_qonto.base_url (https://thirdparty-sandbox.staging.qonto.co/v2/)
        # et expense_qonto.staging_token (en-tête X-Qonto-Staging-Token). Sans eux, c'est la production.
        params_sys = self.env['ir.config_parameter'].sudo()
        headers = {'Authorization': '%s:%s' % (company.statement_import_api_login, company.statement_import_api_password)}
        if params_sys.get_param('expense_qonto.staging_token'):
            headers['X-Qonto-Staging-Token'] = params_sys.get_param('expense_qonto.staging_token')
        base_url = params_sys.get_param('expense_qonto.base_url') or BASE_URL
        response = requests.request(
            method, base_url.rstrip('/') + '/' + path, params=params or {}, json=payload, timeout=TIMEOUT, headers=headers)
        if response.status_code not in ok:
            raise UserError(_("L'API Qonto a répondu %(code)s pour %(path)s : %(detail)s",
                              code=response.status_code, path=path, detail=(response.text or '')[:300]))
        return response.json() if response.content else {}

    @api.model
    def _get(self, company, path, params=None):
        return self._call(company, 'GET', path, params=params)

    @api.model
    def create_webhook_subscription(self, company, callback_url, secret, types):
        """Inscrit le webhook ; retourne l'abonnement créé (le secret choisi par nous n'est pas renvoyé en clair
        ailleurs qu'ici, d'où l'envoi du nôtre)."""
        data = self._call(company, 'POST', 'webhook_subscriptions', ok=(200, 201), payload={
            'callback_url': callback_url, 'types': types, 'secret': secret,
            'description': 'Odoo : dépenses par carte'})
        return data.get('webhook_subscription') or data.get('subscription') or data

    @api.model
    def delete_webhook_subscription(self, company, subscription_id):
        self._call(company, 'DELETE', 'webhook_subscriptions/%s' % subscription_id, ok=(200, 204))

    @api.model
    def get_transaction(self, company, transaction_id):
        return self._get(company, 'transactions/%s' % transaction_id).get('transaction') or {}

    @api.model
    def list_transactions(self, company, bank_account_id, updated_at_from=None):
        """Transactions par carte du compte, modifiées depuis la date donnée (toutes les pages)."""
        params = {'bank_account_id': bank_account_id, 'operation_type[]': 'card', 'side': 'debit', 'per_page': 100}
        if updated_at_from:
            params['updated_at_from'] = updated_at_from.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        transactions, page, total_pages = [], 1, 1
        while page <= total_pages:
            data = self._get(company, 'transactions', dict(params, current_page=page))
            transactions += data.get('transactions', [])
            total_pages = (data.get('meta') or {}).get('total_pages') or 1
            page += 1
        return transactions
