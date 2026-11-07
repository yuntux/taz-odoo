import logging
import time

import requests

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Entités SData utilisées par ce connecteur. Le chemin de chaque entité porte son propre "dataset"
# (ex: "gentiers/...", "cptimport/...") -- ne jamais le faire précéder d'un dataset fixé ailleurs,
# le contrat (SDataClient.contract_code) est le seul segment commun à toutes les URLs.
SAGE_ENTITY_PARTNER = "gentiers/TTiers"
SAGE_ENTITY_PARTNER_CLIENT = "gentiers/TClient"
SAGE_ENTITY_PARTNER_VENDOR = "gentiers/TFournisseur"

SAGE_ENTITY_JOURNAL_IMPORT_SESSION = "cptimport/tsessionimport"
SAGE_ENTITY_JOURNAL_IMPORT_ENTRY = "cptimport/timportecritures"
SAGE_ENTITY_JOURNAL_IMPORT_EXECUTE = "cptimportmouvement/TProcessusPlanifImport/Executer"

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 5


class SageConnectorError(Exception):
    """Raised when a call to the Sage SData API fails (network, HTTP status, bad payload)."""


class SDataClient:
    """Minimal SData HTTP client for Sage FRP 1000.

    Authentication: static Bearer token, read fresh from the company record on every
    instantiation (it may be rotated on Sage's side at any time).
    """

    def __init__(self, env, company=None):
        self.env = env
        company = company or env.company
        self.base_url = company.sage_api_endpoint
        self.token = company.sage_api_token
        self.contract_code = company.sage_contract_code
        if not self.base_url or not self.token or not self.contract_code:
            raise UserError(_(
                "Connecteur Sage inactif pour la société %(company)s : l'URL de l'API, le token "
                "ou le code contrat SData ne sont pas tous renseignés (Comptabilité > "
                "Configuration > Sage FRP 1000).",
                company=company.display_name,
            ))
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': 'Bearer %s' % self.token,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        })

    def _url(self, entity):
        return "%s/compta/server/sdata/%s/%s" % (
            self.base_url.rstrip('/'), self.contract_code.strip('/'), entity,
        )

    def _request(self, method, entity, resource_id=None, params=None, json=None):
        url = self._url(entity)
        if resource_id is not None:
            url = "%s('%s')" % (url, resource_id)

        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.request(
                    method, url, params=params, json=json, timeout=DEFAULT_TIMEOUT,
                )
            except requests.RequestException as exc:
                raise SageConnectorError(
                    "Sage API unreachable (%s %s): %s" % (method, url, exc)
                ) from exc

            if response.status_code in (429, 500, 502, 503, 504) and attempt <= MAX_RETRIES:
                _logger.info(
                    "Sage API %s on %s %s, retry %s/%s in %ss",
                    response.status_code, method, url, attempt, MAX_RETRIES, RETRY_BACKOFF_SECONDS,
                )
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue

            if not response.ok:
                raise SageConnectorError(
                    "Sage API error %s on %s %s: %s" % (
                        response.status_code, method, url, response.text[:500],
                    )
                )
            return response

    def get(self, entity, params=None):
        response = self._request('GET', entity, params=params)
        return response.json() if response.content else {}

    def get_all_pages(self, entity, params=None, max_pages=20):
        """Follow `$nextUrl` until exhausted (or `max_pages` reached), returning the concatenated
        `$resources` across all pages. A capped loop: an abnormally high result count for a
        targeted search (SIRET, VAT, code) signals a search criterion that is too broad, or a
        server-side issue -- not something to page through silently forever.
        """
        resources = []
        data = self.get(entity, params=params)
        resources.extend(data.get('$resources', []) if isinstance(data, dict) else [])
        next_url = data.get('$nextUrl') if isinstance(data, dict) else None
        pages = 1
        while next_url and pages < max_pages:
            response = self.session.request('GET', next_url, timeout=DEFAULT_TIMEOUT)
            if not response.ok:
                break
            data = response.json() if response.content else {}
            resources.extend(data.get('$resources', []))
            next_url = data.get('$nextUrl')
            pages += 1
        if next_url:
            _logger.warning(
                "Sage API: result pagination capped at %s pages for %s (params=%s), "
                "some results may be missing", max_pages, entity, params,
            )
        return resources

    def create(self, entity, data):
        response = self._request('POST', entity, json=data)
        return response.json() if response.content else {}

    def update(self, entity, resource_id, data):
        response = self._request('PUT', entity, resource_id=resource_id, json=data)
        return response.json() if response.content else {}

    def delete(self, entity, resource_id):
        self._request('DELETE', entity, resource_id=resource_id)

    def batch(self, operations, atomic=True):
        """Send a `$batch` request: multiple SData operations in one call.

        Envelope: {"$": {"$transaction": ["atomic"]}, "$resources": [operation, ...]}. Each
        operation carries its own SData control keys ($httpMethod, $url, $uuid, $shareLevel...)
        alongside its business fields. A `$uuid` on one operation can be referenced from a later
        operation in the same batch (e.g. "gentiers/TTiers(newTiers)/creerClient", or
        "SessionImport": {"$uuid": "..."}).

        Note: `atomic` only guarantees that the creation of the objects in this batch succeeds or
        fails as a whole -- it does NOT make the business processing of an EXEC operation atomic
        across the pieces it processes (each piece is accepted/rejected independently there).
        """
        payload = {'$': {'$transaction': ['atomic'] if atomic else []}, '$resources': operations}
        response = self._request('POST', '$batch', json=payload)
        return response.json() if response.content else {}
