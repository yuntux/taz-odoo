import logging
import re
import traceback
import unicodedata

from odoo import _, api, fields, models
from odoo.addons.project_accounting.models.res_partner import PROTECTED_FIELD_LIST

from .sage_client import (
    SAGE_ENTITY_PARTNER,
    SAGE_ENTITY_PARTNER_CLIENT,
    SAGE_ENTITY_PARTNER_VENDOR,
    SDataClient,
)

_logger = logging.getLogger(__name__)

# Une fiche "protégée" (déjà facturée) ne doit pouvoir être modifiée, codes Sage inclus, que par un
# ADV -- mêmes règles que les autres données comptables du tiers (cf. project_accounting).
PROTECTED_FIELD_LIST.extend(['sage_client_code', 'sage_vendor_code'])

# Jointure SData à utiliser pour chaque critère exact de la cascade de recherche (§8.1).
_SAGE_JOIN_FIELD_BY_CRITERION = {
    'siret': 'tiers.codeSIRET',
    'vat': 'tiers.numeroTVACEE',
}


class ResPartner(models.Model):
    _inherit = 'res.partner'

    sage_client_code = fields.Char(string="Code tiers Sage client", readonly=True, copy=False)
    sage_vendor_code = fields.Char(string="Code tiers Sage fournisseur", readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Payload (mapping tiers comptables, SPEC.md §7bis)
    # ------------------------------------------------------------------

    def _sage_build_payload(self):
        self.ensure_one()
        caption = self.name
        if not self.is_company and self.first_name:
            caption = "%s %s" % (self.first_name, self.name)
        return {
            'caption': caption,
            'sitesList': [{
                'code': 'PRINCIPAL',
                'caption': caption,
                'adresse': {
                    'nomRueVoie': self.street,
                    'ville': self.city,
                    'codePostal': self.zip,
                    'pays': {'$url': "TPays(code eq '%s')" % self.country_id.code} if self.country_id else None,
                },
            }],
        }

    def _sage_normalize_root(self):
        self.ensure_one()
        text = unicodedata.normalize('NFKD', self.name or '').encode('ascii', 'ignore').decode('ascii')
        return re.sub(r'[^A-Za-z0-9]', '', text).upper()

    def _sage_default_code(self, role):
        self.ensure_one()
        prefix = '411' if role == 'client' else '401'
        return prefix + self._sage_normalize_root()

    # ------------------------------------------------------------------
    # Cascade de recherche (§8.1)
    # ------------------------------------------------------------------

    def _sage_try_exact_criterion(self, client, criterion, value):
        """Interroge TClient et TFournisseur par jointure sur `criterion`, remplit chaque code de
        rôle encore manquant si un résultat est trouvé. Ne touche jamais un code déjà renseigné.
        """
        self.ensure_one()
        join_field = _SAGE_JOIN_FIELD_BY_CRITERION[criterion]
        if not self.sage_client_code:
            resources = client.get_all_pages(
                SAGE_ENTITY_PARTNER_CLIENT, params={'where': "%s eq '%s'" % (join_field, value)},
            )
            if resources:
                self.sage_client_code = resources[0].get('Caption')
        if not self.sage_vendor_code:
            resources = client.get_all_pages(
                SAGE_ENTITY_PARTNER_VENDOR, params={'where': "%s eq '%s'" % (join_field, value)},
            )
            if resources:
                self.sage_vendor_code = resources[0].get('Caption')

    def _sage_search_cascade_auto(self):
        """Recherche automatique et silencieuse, déclenchée à la validation d'une pièce (§8.0
        point 1). Ne tente QUE les critères exacts (SIRET, TVA) -- jamais la recherche par nom,
        qui nécessite une confirmation humaine (cf. `_sage_search_cascade_manual`).
        """
        self.ensure_one()
        if self.sage_client_code and self.sage_vendor_code:
            return
        try:
            client = SDataClient(self.env)
        except Exception:
            _logger.warning("Sage connector: unable to build API client for partner search")
            _logger.warning(traceback.format_exc())
            return
        for criterion, value in (('siret', self.siret), ('vat', self.vat)):
            if not value:
                continue
            try:
                self._sage_try_exact_criterion(client, criterion, value)
            except Exception:
                _logger.warning("Sage connector: search failed for criterion %s", criterion)
                _logger.warning(traceback.format_exc())
            if self.sage_client_code and self.sage_vendor_code:
                return

    def _sage_search_by_name(self, client, role_entity):
        self.ensure_one()
        if self.is_company:
            where = "tiers.raisonSociale eq '%s'" % self.name
        else:
            where = "tiers.nom eq '%s' and tiers.prenom eq '%s'" % (self.name or '', self.first_name or '')
        return client.get_all_pages(role_entity, params={'where': where})

    def _sage_search_cascade_manual(self):
        """Cascade complète (§8.1), utilisée par le bouton manuel (§8.0 point 2) : critères exacts
        (SIRET, TVA) puis recherche par nom. Renvoie, pour chaque rôle encore manquant après les
        critères exacts, la liste des candidats trouvés par nom (vide si aucun) -- à faire
        confirmer par un humain avant tout rattachement (§8.1 critère 4).

        TODO critère "matricule" (personnes physiques) non implémenté : champs Odoo et Sage non
        identifiés (cf. SPEC.md §3/§8.1).
        """
        self.ensure_one()
        client = SDataClient(self.env)
        for criterion, value in (('siret', self.siret), ('vat', self.vat)):
            if value and not (self.sage_client_code and self.sage_vendor_code):
                self._sage_try_exact_criterion(client, criterion, value)

        candidates = {}
        if not self.sage_client_code:
            candidates['client'] = self._sage_search_by_name(client, SAGE_ENTITY_PARTNER_CLIENT)
        if not self.sage_vendor_code:
            candidates['vendor'] = self._sage_search_by_name(client, SAGE_ENTITY_PARTNER_VENDOR)
        return candidates

    # ------------------------------------------------------------------
    # Création d'un rôle (§8.2)
    # ------------------------------------------------------------------

    def _sage_create_role(self, role, code=None):
        """Crée le tiers (POST gentiers/TTiers) puis le rôle demandé (EXEC creerClient/
        creerFournisseur) dans un même $batch, et relit le Caption réel du rôle créé (Sage peut
        re-préfixer le code fourni, cf. SPEC.md §8.2) avant de l'enregistrer.
        """
        self.ensure_one()
        client = SDataClient(self.env)
        code = code or self._sage_default_code(role)
        payload = dict(self._sage_build_payload(), code=code)
        exec_method = 'creerClient' if role == 'client' else 'creerFournisseur'
        operations = [
            {'$httpMethod': 'POST', '$url': SAGE_ENTITY_PARTNER, '$uuid': 'newTiers', '$shareLevel': 2, **payload},
            {'$httpMethod': 'EXEC', '$url': '%s(newTiers)/%s' % (SAGE_ENTITY_PARTNER, exec_method)},
        ]
        client.batch(operations)
        role_entity = SAGE_ENTITY_PARTNER_CLIENT if role == 'client' else SAGE_ENTITY_PARTNER_VENDOR
        actual_caption = self._sage_reread_role_caption(client, role_entity, code)
        field_name = 'sage_client_code' if role == 'client' else 'sage_vendor_code'
        self.write({field_name: actual_caption or code})
        return actual_caption or code

    @api.model
    def _sage_reread_role_caption(self, client, role_entity, code):
        resources = client.get_all_pages(role_entity, params={'where': "tiers.code eq '%s'" % code})
        return resources[0].get('Caption') if resources else None

    # ------------------------------------------------------------------
    # Déclencheur manuel (§8.0 point 2)
    # ------------------------------------------------------------------

    def action_sage_resolve(self):
        self.ensure_one()
        missing = [role for role in ('client', 'vendor') if not self['sage_%s_code' % role]]
        if not missing:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Sage"),
                    'message': _("Ce tiers est résolu sur Sage (client et/ou fournisseur)."),
                    'type': 'success',
                },
            }

        candidates = self._sage_search_cascade_manual()
        missing = [role for role in ('client', 'vendor') if not self['sage_%s_code' % role]]
        if not missing:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Sage"),
                    'message': _("Tiers résolu automatiquement par recherche exacte (SIRET/TVA)."),
                    'type': 'success',
                },
            }

        wizard_ids = []
        for role in missing:
            wizard = self.env['sage.partner.resolve.wizard'].create({
                'partner_id': self.id,
                'role': role,
                'new_code': self._sage_default_code(role),
                'candidate_ids': [(0, 0, {
                    'code': candidate.get('Caption'),
                    'name': candidate.get('raisonSociale') or candidate.get('Caption'),
                }) for candidate in candidates.get(role, [])],
            })
            wizard_ids.append(wizard.id)

        return {
            'type': 'ir.actions.act_window',
            'name': _("Résolution du tiers sur Sage"),
            'res_model': 'sage.partner.resolve.wizard',
            'view_mode': 'form' if len(wizard_ids) == 1 else 'list,form',
            'res_id': wizard_ids[0] if len(wizard_ids) == 1 else False,
            'domain': [('id', 'in', wizard_ids)],
            'target': 'new',
        }
