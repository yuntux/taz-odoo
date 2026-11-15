import logging
from datetime import timedelta
from urllib.parse import urlencode

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules import module

_logger = logging.getLogger(__name__)

BASE_URL = "https://thirdparty.qonto.com/v2/"
SANDBOX_URL = "https://thirdparty-sandbox.staging.qonto.co/v2/"
OAUTH_HOST = "https://oauth.qonto.com"
OAUTH_SANDBOX_HOST = "https://oauth-sandbox.staging.qonto.co"
TIMEOUT = 20


class QontoApi(models.AbstractModel):
    _name = 'qonto.api'
    _description = "Client de l'API Qonto"

    @api.model
    def _staging_headers(self, company):
        """En-tête du bac à sable (jeton de staging), vide en production. Le jeton est contrôlé ici et non à
        l'enregistrement : l'écran des paramètres écrit l'environnement et le jeton champ par champ."""
        if company.qonto_environment != 'sandbox':
            return {}
        if not company.qonto_staging_token:
            raise UserError(_("Le jeton de staging est obligatoire dans l'environnement bac à sable de Qonto (société %s).",
                              company.name))
        return {'X-Qonto-Staging-Token': company.qonto_staging_token}

    @api.model
    def _oauth_urls(self, company):
        """(adresse d'autorisation, adresse des jetons) de Qonto, selon l'environnement."""
        host = OAUTH_SANDBOX_HOST if company.qonto_environment == 'sandbox' else OAUTH_HOST
        return host + '/oauth2/auth', host + '/oauth2/token'

    @api.model
    def _oauth_authorization_url(self, company, state):
        """Adresse de la page d'autorisation de Qonto, où le navigateur de l'administrateur est envoyé (jamais appelée
        par le serveur : Qonto lie la réponse au navigateur)."""
        self._staging_headers(company)  # refuse un bac à sable sans jeton avant d'envoyer l'administrateur chez Qonto
        query = urlencode({
            'client_id': company.qonto_oauth_client_id, 'redirect_uri': company.qonto_oauth_redirect_uri,
            'response_type': 'code', 'scope': ' '.join(company._qonto_oauth_scope_codes()), 'state': state})
        return self._oauth_urls(company)[0] + '?' + query

    @api.model
    def _oauth_token_request(self, company, data):
        """Demande de jetons à Qonto : formulaire POST, identifiants du client dans le corps (jamais en Basic)."""
        data = dict(data, client_id=company.qonto_oauth_client_id, client_secret=company.qonto_oauth_client_secret)
        response = requests.post(
            self._oauth_urls(company)[1], data=data, timeout=TIMEOUT,
            headers=dict(self._staging_headers(company), **{'Content-Type': 'application/x-www-form-urlencoded'}))
        if response.status_code != 200:
            raise UserError(_("Qonto a refusé la demande de jeton (%(code)s) : %(detail)s",
                              code=response.status_code, detail=(response.text or '')[:300]))
        return response.json()

    @api.model
    def _oauth_exchange_code(self, company, code):
        return self._oauth_token_request(company, {
            'grant_type': 'authorization_code', 'code': code, 'redirect_uri': company.qonto_oauth_redirect_uri})

    @api.model
    def _oauth_access_token(self, company):
        """Jeton d'accès valable ; renouvelé si besoin avec le jeton de renouvellement, qui est à usage unique : le
        nouveau est enregistré tout de suite (et validé en base), sans quoi un échec plus loin le ferait perdre."""
        if not company.qonto_refresh_token:
            raise UserError(_("Qonto n'est pas connecté pour la société %s : connectez-vous dans les paramètres.",
                              company.name))
        expiry = company.qonto_access_token_expiry
        if company.qonto_access_token and expiry and expiry > fields.Datetime.now() + timedelta(minutes=5):
            return company.qonto_access_token
        tokens = self._oauth_token_request(company, {
            'grant_type': 'refresh_token', 'refresh_token': company.qonto_refresh_token})
        company._qonto_store_tokens(tokens)
        if not getattr(module, 'current_test', None):  # un test ne peut pas valider la transaction
            self.env.cr.commit()
        return company.qonto_access_token

    @api.model
    def _call(self, company, method, path, params=None, payload=None, ok=(200,)):
        """Appel authentifié (OAuth 2.0, ou clé d'API identifiant:secret) ; retourne le JSON, ou {} sans corps."""
        company = company.sudo()
        # Le bac à sable appelle l'API de test avec le jeton de staging.
        headers = self._staging_headers(company)
        if company.qonto_auth_mode == 'oauth':
            headers['Authorization'] = 'Bearer %s' % self._oauth_access_token(company)
        else:
            if not (company.statement_import_api_login and company.statement_import_api_password):
                raise UserError(_("Les identifiants de l'API Qonto ne sont pas renseignés sur la société %s.", company.name))
            headers['Authorization'] = '%s:%s' % (company.statement_import_api_login, company.statement_import_api_password)
        base_url = SANDBOX_URL if company.qonto_environment == 'sandbox' else BASE_URL
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
