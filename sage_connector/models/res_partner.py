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
        """Recherche par nom, critère de dernier recours (§8.1) -- limite connue et acceptée :
        sensible à la casse de la *valeur* comparée (Odoo stocke en majuscules, Sage en casse
        mixte), ni `eq` ni `like` ne contournent ça côté Sage (cf. SPEC.md §8.1 détail critère 4).
        """
        self.ensure_one()
        if self.is_company:
            where = "tiers.raisonSociale eq '%s'" % self.name
        else:
            where = "tiers.nom eq '%s' and tiers.prenom eq '%s'" % (self.name or '', self.first_name or '')
        return client.get_all_pages(role_entity, params={'where': where})

    def _sage_resolve_exact_for_role(self, client, field_name, entity):
        """Critères exacts (SIRET, TVA) pour un rôle donné, utilisé par le bouton manuel (§8.0
        point 2) -- contrairement à `_sage_try_exact_criterion` (cascade automatique, silencieuse),
        celui-ci rend compte du résultat même quand le code est déjà renseigné : un tiers trouvé sur
        Sage mais différent du code existant doit être signalé, pas silencieusement ignoré.

        TODO critère "matricule" (personnes physiques) non implémenté : champs Odoo et Sage non
        identifiés (cf. SPEC.md §3/§8.1).

        Renvoie (status, code_trouvé) avec status parmi 'filled' (était vide, rempli), 'differs'
        (déjà rempli, Sage renvoie autre chose), 'same' (déjà rempli, Sage confirme), 'not_found'.
        """
        self.ensure_one()
        for criterion, value in (('siret', self.siret), ('vat', self.vat)):
            if not value:
                continue
            join_field = _SAGE_JOIN_FIELD_BY_CRITERION[criterion]
            resources = client.get_all_pages(entity, params={'where': "%s eq '%s'" % (join_field, value)})
            if resources:
                found_code = resources[0].get('Caption')
                current = self[field_name]
                if not current:
                    self.write({field_name: found_code})
                    return 'filled', found_code
                return ('same', found_code) if current == found_code else ('differs', found_code)
        return 'not_found', None

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

    @api.model
    def _sage_notification(self, message, notification_type):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _("Sage"), 'message': message, 'type': notification_type},
        }

    def action_sage_resolve(self):
        """Bouton manuel (§8.0 point 2). Pour chaque rôle (client/fournisseur) déjà résolu, on
        vérifie quand même les critères exacts (SIRET/TVA) pour détecter un écart sans jamais
        écraser le code existant (alerte si Sage renvoie autre chose). Pour chaque rôle encore
        manquant : recherche par nom, candidats affichés dans le wizard pour confirmation humaine
        (§8.1 critère 4) -- jamais de rattachement automatique sur un simple match de nom.
        """
        self.ensure_one()
        if self.sage_client_code and self.sage_vendor_code:
            return self._sage_notification(
                _("Ce tiers est déjà résolu sur Sage (client et fournisseur)."), 'success',
            )

        client = SDataClient(self.env)
        role_info = {
            'client': ('sage_client_code', SAGE_ENTITY_PARTNER_CLIENT),
            'vendor': ('sage_vendor_code', SAGE_ENTITY_PARTNER_VENDOR),
        }
        exact_results = {
            role: self._sage_resolve_exact_for_role(client, field_name, entity)
            for role, (field_name, entity) in role_info.items()
        }

        if self.sage_client_code and self.sage_vendor_code:
            differs = [role for role, (status, _code) in exact_results.items() if status == 'differs']
            if differs:
                return self._sage_notification(_(
                    "Tiers résolu, mais Sage renvoie un code %(role)s différent par SIRET/TVA -- "
                    "vérifiez manuellement sur Sage avant de changer quoi que ce soit.",
                    role=" et ".join(differs),
                ), 'warning')
            return self._sage_notification(_("Tiers résolu sur Sage (client et fournisseur)."), 'success')

        line_vals = []
        for role, (field_name, entity) in role_info.items():
            status, found_code = exact_results[role]
            if self[field_name]:
                line_vals.append((0, 0, {
                    'partner_id': self.id, 'role': role, 'resolved': True,
                    'code': self[field_name], 'name': _("Déjà résolu"),
                }))
                if status == 'differs':
                    line_vals.append((0, 0, {
                        'partner_id': self.id, 'role': role, 'resolved': True,
                        'code': found_code,
                        'name': _("⚠ Sage renvoie un code différent par SIRET/TVA -- à vérifier manuellement"),
                    }))
                continue
            found = self._sage_search_by_name(client, entity)
            if found:
                line_vals.extend((0, 0, {
                    'partner_id': self.id, 'role': role,
                    'code': candidate.get('Caption'),
                    'name': candidate.get('raisonSociale') or candidate.get('Caption'),
                }) for candidate in found)
            else:
                line_vals.append((0, 0, {
                    'partner_id': self.id, 'role': role, 'is_create_option': True,
                    'new_code': self._sage_default_code(role),
                }))

        batch = self.env['sage.partner.resolve.batch.wizard'].create({'line_ids': line_vals})
        return {
            'type': 'ir.actions.act_window',
            'name': _("Résolution du tiers sur Sage"),
            'res_model': 'sage.partner.resolve.batch.wizard',
            'view_mode': 'form',
            'res_id': batch.id,
            'target': 'new',
        }
