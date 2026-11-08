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
    def _sage_collect_diagnoses(node):
        """Parcours récursif générique de l'objet JSON à la recherche de toute clé `$diagnoses`,
        à n'importe quelle profondeur. Remplace une liste d'emplacements codés en dur au fil des
        incidents constatés en prod (racine `$`, `$batch/$`, `$resources[]/$`) -- cette liste
        s'étant déjà révélée incomplète deux fois (diagnostic non détecté, fallback texte brut
        utilisé par chance), un parcours générique couvre aussi tout emplacement futur non prévu.
        """
        found = []
        if isinstance(node, dict):
            diagnoses = node.get('$diagnoses')
            if diagnoses:
                found.extend(diagnoses)
            for value in node.values():
                found.extend(SDataClient._sage_collect_diagnoses(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(SDataClient._sage_collect_diagnoses(item))
        return found

    @classmethod
    def _describe_error_body(cls, response):
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

        diagnoses = cls._sage_collect_diagnoses(data)
        # Une ressource en échec dans un `$batch` peut n'avoir aucun `$diagnoses` du tout, juste un
        # `$httpStatus` >= 400 -- dans ce cas, inclure la ressource elle-même (déjà couvert par le
        # diagnostic nested sinon) pour ne pas revenir aveuglément au texte brut tronqué.
        if isinstance(data, dict):
            for resource in data.get('$resources') or []:
                if not isinstance(resource, dict) or (resource.get('$httpStatus') or 0) < 400:
                    continue
                nested = resource.get('$') if isinstance(resource.get('$'), dict) else {}
                if not nested.get('$diagnoses'):
                    diagnoses.append(resource)

        if not diagnoses:
            return response.text[:2000]
        return json.dumps(diagnoses, ensure_ascii=False)[:2000]

    def _send(self, method, url, json=None):
        """Boucle d'envoi partagée par `_request` (première page / tout appel simple) et par la
        pagination de `get_all_pages` (pages suivantes) -- pour que les deux aient exactement la
        même gestion d'erreur/retry au lieu que la pagination réimplique sa propre version
        divergente (constaté : elle masquait silencieusement une vraie erreur HTTP sous un
        avertissement trompeur de "limite de pages atteinte").

        Retry uniquement sur `GET` (idempotent) : `POST`/`EXEC`/`PUT` (création de tiers, import
        d'écritures) ne le sont pas -- une réponse perdue après un traitement déjà effectué côté
        Sage ne doit pas être retentée automatiquement, au risque de créer un doublon.
        """
        attempt = 0
        while True:
            attempt += 1
            _logger.info(self._as_curl_command(method, url, json))
            try:
                response = self.session.request(method, url, json=json, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as exc:
                raise SageConnectorError(
                    "Sage API unreachable (%s %s): %s" % (method, url, exc)
                ) from exc

            _logger.info(
                "Sage API response: %s %s -> %s\n%s",
                method, url, response.status_code, response.text[:2000],
            )

            retryable = method == 'GET' and response.status_code in (429, 500, 502, 503, 504)
            if retryable and attempt <= MAX_RETRIES:
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

    def _request(self, method, entity, resource_id=None, params=None, json=None):
        url = self._url(entity)
        if resource_id is not None:
            url = "%s('%s')" % (url, resource_id)
        if params:
            # requests/urlencode encodent l'espace en "+" par défaut -- Sage ne le décode pas (son
            # parseur de filtre `where=` le prend au pied de la lettre, cf. erreur 400 observée sur
            # le bac à sable), il faut du "%20" (quote_via=quote).
            url = "%s?%s" % (url, urlencode(params, quote_via=quote))
        return self._send(method, url, json=json)

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
            # Passe par `_send` (même retry/gestion d'erreur que la première page) au lieu d'un
            # appel direct à `self.session.request` -- une erreur HTTP sur une page suivante lève
            # maintenant `SageConnectorError` comme la première page, au lieu d'être masquée sous
            # un avertissement générique de "limite de pages atteinte".
            response = self._send('GET', next_url)
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
