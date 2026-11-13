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
    def _get(self, company, path, params=None):
        """Appel GET authentifié par la clé d'API de la société (identifiant:secret)."""
        company = company.sudo()
        if not (company.statement_import_api_login and company.statement_import_api_password):
            raise UserError(_("Les identifiants de l'API Qonto ne sont pas renseignés sur la société %s.", company.name))
        response = requests.get(
            BASE_URL + path, params=params or {}, timeout=TIMEOUT,
            headers={'Authorization': '%s:%s' % (company.statement_import_api_login, company.statement_import_api_password)})
        if response.status_code != 200:
            raise UserError(_("L'API Qonto a répondu %(code)s pour %(path)s.", code=response.status_code, path=path))
        return response.json()

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
