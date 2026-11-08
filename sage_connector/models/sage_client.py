import json
import logging
import shlex
import time
from urllib.parse import quote, urlencode

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

    def _as_curl_command(self, method, url, body):
        """Journalise l'appel sous forme de commande `curl` copiable-collable telle quelle dans un
        script minimaliste pour rejouer/comprendre une erreur (SPEC.md -- débogage en prod). Le
        token Bearer est volontairement masqué (jamais en clair dans un fichier de log qui peut
        être copié/collé ailleurs, y compris dans cette conversation) : à remplacer par la valeur
        de `res.company.sage_api_token` (Comptabilité > Configuration > Sage FRP 1000).
        """
        parts = ["curl -X %s %s" % (method, shlex.quote(url))]
        for key, value in self.session.headers.items():
            if key == 'Authorization':
                value = 'Bearer ***SAGE_API_TOKEN***'
            parts.append("  -H %s" % shlex.quote("%s: %s" % (key, value)))
        if body is not None:
            parts.append("  -d %s" % shlex.quote(json.dumps(body, ensure_ascii=False)))
        return " \\\n".join(parts)

    @staticmethod
    def _describe_error_body(response):
        """Résumé exploitable d'une erreur Sage : un simple `response.text[:500]` coupe souvent
        avant le `$diagnoses`/`$messages` qui explique réellement le refus (constaté en prod sur un
        `$batch` dont la session et plusieurs lignes avaient réussi (`$httpStatus` 201) avant que le
        texte ne soit tronqué pile avant l'opération en échec). On cherche spécifiquement les
        diagnostics plutôt que de tronquer aveuglément.
        """
        try:
            data = json.loads(response.text)
        except ValueError:
            return response.text[:2000]

        diagnoses = []
        top_level = data.get('$') if isinstance(data, dict) else None
        if isinstance(top_level, dict) and top_level.get('$diagnoses'):
            diagnoses.extend(top_level['$diagnoses'])
        # Un rollback de `$batch` (toutes les opérations réussies individuellement, mais annulées
        # globalement) porte son diagnostic sous une clé `$batch` dédiée, pas sous `$` -- constaté
        # en prod sur un rejet "Un tiers possédant le même code existe déjà" (§8.2), jamais détecté
        # avant ce correctif, seul le texte brut (par chance sous 2000 caractères) le montrait.
        batch_level = data.get('$batch') if isinstance(data, dict) else None
        if isinstance(batch_level, dict):
            nested = batch_level.get('$') if isinstance(batch_level.get('$'), dict) else {}
            if nested.get('$diagnoses'):
                diagnoses.extend(nested['$diagnoses'])
        for resource in (data.get('$resources') or []) if isinstance(data, dict) else []:
            if not isinstance(resource, dict):
                continue
            # Le diagnostic d'une ressource en échec dans un `$batch` est niché sous sa propre clé
            # `$` (ex. {"$milliseconds": ..., "$": {"$diagnoses": [...]}}), PAS directement sous
            # `$diagnoses` au premier niveau de la ressource -- erreur constatée en prod : le
            # diagnostic n'était pas détecté, seul le fallback texte brut (sous 2000 caractères
            # par chance) l'a laissé visible.
            nested = resource.get('$') if isinstance(resource.get('$'), dict) else {}
            if nested.get('$diagnoses') or (resource.get('$httpStatus') or 0) >= 400:
                diagnoses.append(resource)

        if not diagnoses:
            return response.text[:2000]
        return json.dumps(diagnoses, ensure_ascii=False)[:2000]

    def _request(self, method, entity, resource_id=None, params=None, json=None):
        url = self._url(entity)
        if resource_id is not None:
            url = "%s('%s')" % (url, resource_id)
        if params:
            # requests/urlencode encodent l'espace en "+" par défaut -- Sage ne le décode pas (son
            # parseur de filtre `where=` le prend au pied de la lettre, cf. erreur 400 observée sur
            # le bac à sable), il faut du "%20" (quote_via=quote).
            url = "%s?%s" % (url, urlencode(params, quote_via=quote))
            params = None

        attempt = 0
        while True:
            attempt += 1
            _logger.info(self._as_curl_command(method, url, json))
            try:
                response = self.session.request(
                    method, url, params=params, json=json, timeout=DEFAULT_TIMEOUT,
                )
            except requests.RequestException as exc:
                raise SageConnectorError(
                    "Sage API unreachable (%s %s): %s" % (method, url, exc)
                ) from exc

            _logger.info(
                "Sage API response: %s %s -> %s\n%s",
                method, url, response.status_code, response.text[:2000],
            )

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
                        response.status_code, method, url, self._describe_error_body(response),
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
            _logger.info(self._as_curl_command('GET', next_url, None))
            response = self.session.request('GET', next_url, timeout=DEFAULT_TIMEOUT)
            _logger.info(
                "Sage API response: GET %s -> %s\n%s",
                next_url, response.status_code, response.text[:2000],
            )
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
