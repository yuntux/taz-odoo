import logging
import re
import traceback

from unidecode import unidecode

from odoo import _, api, fields, models
from odoo.addons.project_accounting.models.res_partner import PROTECTED_FIELD_LIST
from odoo.exceptions import AccessError, UserError, ValidationError

from .sage_client import (
    SAGE_ENTITY_PARTNER,
    SAGE_ENTITY_PARTNER_CLIENT,
    SAGE_ENTITY_PARTNER_VENDOR,
    SDataClient,
    SageConnectorError,
)

_logger = logging.getLogger(__name__)

# Une fiche "protégée" (déjà facturée) ne doit pouvoir être modifiée, codes Sage inclus, que par un
# ADV -- mêmes règles que les autres données comptables du tiers (cf. project_accounting).
PROTECTED_FIELD_LIST.extend(['sage_client_code', 'sage_vendor_code'])

# Jointure SData à utiliser pour chaque critère exact de la cascade de recherche (§7.4.2), dans
# l'ordre où ils sont essayés : SIRET, puis SIREN (noSiren), puis TVA intracommunautaire.
_SAGE_JOIN_FIELD_BY_CRITERION = {
    'siret': 'tiers.codeSIRET',
    'siren': 'tiers.noSiren',
    'vat': 'tiers.numeroTVACEE',
}


class ResPartner(models.Model):
    _inherit = 'res.partner'

    sage_client_code = fields.Char(string="Code tiers Sage client", readonly=True, copy=False)
    sage_vendor_code = fields.Char(string="Code tiers Sage fournisseur", readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Payload (mapping tiers comptables, SPEC.md §7.3)
    # ------------------------------------------------------------------

    def _sage_build_payload(self):
        self.ensure_one()
        caption = self.name
        if not self.is_company:
            caption = ("%s %s" % (self.first_name or '', self.name or '')).strip()
        payload = {
            'caption': caption,
            'sitesList': [{
                'code': 'PRINCIPAL',
                'caption': caption,
                'adresse': {
                    'nomRueVoie': self.street,
                    'complementAdresse': self.street2,
                    'indicationDistribution': self.street3,
                    'ville': self.city,
                    'codePostal': self.zip,
                    'pays': {'$url': "TPays(code eq '%s')" % self.country_id.code} if self.country_id else None,
                },
            }],
        }
        # Envoyés seulement s'ils sont renseignés côté Odoo (§7.3.1) -- aident la cascade de
        # recherche (§7.4.2) à retrouver ce tiers la prochaine fois, et évitent de créer un tiers
        # incomplet alors que ces données sont déjà connues.
        if self.siret:
            payload['codeSIRET'] = self.siret
        if self.siren:
            payload['noSiren'] = self.siren
        if self.vat:
            payload['numeroTVACEE'] = self.vat
        return payload

    def _sage_normalize_root(self):
        self.ensure_one()
        return re.sub(r'[^A-Za-z0-9]', '', unidecode(self.name or '')).upper()

    def _sage_default_code(self, role):
        self.ensure_one()
        prefix = '411' if role == 'client' else '401'
        return prefix + self._sage_normalize_root()

    @staticmethod
    def _sage_eq_filter(field, value):
        """Construit une clause `<field> eq '<value>'` en échappant le guillemet simple (doublé,
        convention OData) -- un nom/SIRET/TVA contenant une apostrophe (ex. "L'Oréal") casserait
        sinon la syntaxe du filtre envoyé à Sage.
        """
        escaped = (value or '').replace("'", "''")
        return "%s eq '%s'" % (field, escaped)

    def _sage_exact_criteria(self):
        """Critères exacts de la cascade (§7.4.2), dans l'ordre -- utilisé à la fois par la
        recherche automatique silencieuse et par la cascade manuelle, pour qu'un futur critère
        ajouté à l'un soit automatiquement pris en compte par l'autre.
        """
        self.ensure_one()
        return (('siret', self.siret), ('siren', self.siren), ('vat', self.vat))

    # ------------------------------------------------------------------
    # Cascade de recherche (§7.4.2)
    # ------------------------------------------------------------------

    def _sage_try_exact_criterion(self, client, criterion, value):
        """Interroge TClient et TFournisseur par jointure sur `criterion`, remplit chaque code de
        rôle encore manquant si un résultat *unique* est trouvé (une correspondance ambiguë n'est
        jamais rattachée automatiquement, même chemin que la cascade manuelle `_sage_resolve_role`
        -- §7.4.1 point 1). Ne touche jamais un code déjà renseigné.

        Renvoie `True` si un code a bien été trouvé côté Sage mais n'a pas pu être enregistré côté
        Odoo (fiche protégée, écriture bloquée par `project_accounting` pour un utilisateur non-ADV)
        -- pour que l'appelant puisse distinguer ce cas de "rien trouvé" (cf. `_sage_ensure_partner_code`
        dans `account_move.py`).
        """
        self.ensure_one()
        join_field = _SAGE_JOIN_FIELD_BY_CRITERION[criterion]
        blocked = False
        if not self.sage_client_code:
            resources = client.get_all_pages(
                SAGE_ENTITY_PARTNER_CLIENT, params={'where': self._sage_eq_filter(join_field, value)},
            )
            if len(resources) == 1:
                try:
                    self.sage_client_code = resources[0].get('Caption')
                except ValidationError:
                    blocked = True
        if not self.sage_vendor_code:
            resources = client.get_all_pages(
                SAGE_ENTITY_PARTNER_VENDOR, params={'where': self._sage_eq_filter(join_field, value)},
            )
            if len(resources) == 1:
                try:
                    self.sage_vendor_code = resources[0].get('Caption')
                except ValidationError:
                    blocked = True
        return blocked

    def _sage_search_cascade_auto(self):
        """Recherche automatique et silencieuse, déclenchée à la validation d'une pièce (§7.4.1
        point 1). Ne tente QUE les critères exacts (SIRET, SIREN, TVA) -- jamais la recherche par
        nom, qui nécessite une confirmation humaine (cf. `_sage_resolve_role`, boutons manuels).

        Renvoie `True` si un code a été trouvé sur Sage pour au moins un rôle mais n'a pas pu être
        écrit (fiche protégée) -- cf. `_sage_try_exact_criterion`.
        """
        self.ensure_one()
        if self.sage_client_code and self.sage_vendor_code:
            return False
        try:
            client = SDataClient(self.env)
        except Exception:
            _logger.warning("Sage connector: unable to build API client for partner search")
            _logger.warning(traceback.format_exc())
            return False
        blocked = False
        for criterion, value in self._sage_exact_criteria():
            if not value:
                continue
            try:
                if self._sage_try_exact_criterion(client, criterion, value):
                    blocked = True
            except Exception:
                _logger.warning("Sage connector: search failed for criterion %s", criterion)
                _logger.warning(traceback.format_exc())
            if self.sage_client_code and self.sage_vendor_code:
                return False
        return blocked

    def _sage_search_by_name(self, client, role_entity):
        """Recherche par nom, critère de dernier recours (§7.4.2, grilles personne morale/physique) --
        limite connue et acceptée : sensible à la casse de la *valeur* comparée (Odoo stocke en
        majuscules, Sage en casse mixte), ni `eq` ni `like` ne contournent ça côté Sage (cf.
        SPEC.md §7.4.2).
        """
        self.ensure_one()
        if self.is_company:
            where = self._sage_eq_filter('tiers.raisonSociale', self.name)
        else:
            where = "%s and %s" % (
                self._sage_eq_filter('tiers.nom', self.name),
                self._sage_eq_filter('tiers.prenom', self.first_name),
            )
        return client.get_all_pages(role_entity, params={'where': where})

    def _sage_resolve_role(self, client, field_name, entity):
        """Cascade pour un seul rôle (§7.4.2) : SIRET, puis SIREN, puis TVA, puis (si le rôle est
        encore vide) raison sociale/nom -- s'arrête au **premier** critère qui renvoie quelque
        chose, jamais ne descend plus bas. TODO critère "matricule" (personnes physiques) non
        implémenté : champs Odoo et Sage non identifiés (cf. SPEC.md §4/§7.4.2).

        Renvoie un dict :
        - {'status': 'already_resolved'} : champ déjà rempli, Sage confirme le même code (ou
          aucun des critères exacts n'a rien trouvé de différent) ;
        - {'status': 'filled', 'code': ...} : champ vide, un seul résultat trouvé, code écrit ;
        - {'status': 'differs', 'code': ...} : champ déjà rempli, Sage renvoie un code différent
          (jamais écrasé automatiquement) ;
        - {'status': 'ambiguous', 'candidates': [...]} : plusieurs résultats au même critère,
          confirmation humaine requise (§7.4.2) ;
        - {'status': 'not_found'} : rien trouvé sur aucun critère, proposer la création (§7.4.4).
        """
        self.ensure_one()
        already_resolved = bool(self[field_name])
        for criterion, value in self._sage_exact_criteria():
            if not value:
                continue
            join_field = _SAGE_JOIN_FIELD_BY_CRITERION[criterion]
            resources = client.get_all_pages(entity, params={'where': self._sage_eq_filter(join_field, value)})
            if not resources:
                continue
            if already_resolved:
                found_code = resources[0].get('Caption')
                if found_code == self[field_name]:
                    return {'status': 'already_resolved'}
                return {'status': 'differs', 'code': found_code}
            if len(resources) == 1:
                code = resources[0].get('Caption')
                self.write({field_name: code})
                return {'status': 'filled', 'code': code}
            return {'status': 'ambiguous', 'candidates': resources}

        if already_resolved:
            return {'status': 'already_resolved'}

        found = self._sage_search_by_name(client, entity)
        if not found:
            return {'status': 'not_found'}
        if len(found) == 1:
            code = found[0].get('Caption')
            self.write({field_name: code})
            return {'status': 'filled', 'code': code}
        return {'status': 'ambiguous', 'candidates': found}

    # ------------------------------------------------------------------
    # Création d'un rôle (§7.4.4)
    # ------------------------------------------------------------------

    def _sage_create_role(self, role, code=None):
        """Crée le tiers (POST gentiers/TTiers) puis le rôle demandé (EXEC creerClient/
        creerFournisseur) dans un même $batch, et relit le Caption réel du rôle créé (Sage peut
        re-préfixer le code fourni, cf. SPEC.md §7.4.4) avant de l'enregistrer.
        """
        self.ensure_one()
        client = SDataClient(self.env)
        code = code or self._sage_default_code(role)
        payload = dict(self._sage_build_payload(), code=code)
        exec_method = 'creerClient' if role == 'client' else 'creerFournisseur'
        operations = [
            {'$httpMethod': 'POST', '$url': SAGE_ENTITY_PARTNER, '$uuid': 'newTiers', '$shareLevel': 1, **payload},
            {'$httpMethod': 'EXEC', '$url': '%s(newTiers)/%s' % (SAGE_ENTITY_PARTNER, exec_method)},
        ]
        try:
            client.batch(operations)
        except SageConnectorError as exc:
            if "existe déjà" in str(exc):
                # Un tiers portant ce code existe réellement dans la base Sage, mais la recherche
                # (§7.4.2) ne l'avait trouvé par aucun critère : il existe très probablement, mais
                # n'est pas partagé/accessible pour la société qui appelle (ici TSMN) -- constaté en
                # bac à sable le 2026-11-08. Ce n'est pas un problème que le connecteur peut résoudre
                # lui-même (partage à gérer côté Sage), un humain doit intervenir.
                raise UserError(_(
                    "Impossible de créer le tiers %(code)s : un tiers portant ce code existe déjà "
                    "dans Sage, mais n'est pas partagé avec votre société. Contactez le service "
                    "comptabilité pour qu'il partage ce tiers avec votre société sur Sage, puis "
                    "réessayez.",
                    code=code,
                )) from exc
            # Message générique si le diagnostic ne correspond à aucun cas connu (ou si le texte
            # exact de Sage évolue un jour) -- un humain devant le wizard doit toujours voir un
            # message propre, jamais la trace technique brute. Le détail reste dans les logs
            # serveur pour le débogage (cf. `_logger.warning` ci-dessous).
            _logger.warning("Sage connector: échec de création du tiers %s (%s)", code, exc)
            raise UserError(_(
                "La création du tiers %(code)s sur Sage a échoué. Voir les logs serveur pour le "
                "détail, ou contactez le service comptabilité si le problème persiste.",
                code=code,
            )) from exc
        role_entity = SAGE_ENTITY_PARTNER_CLIENT if role == 'client' else SAGE_ENTITY_PARTNER_VENDOR
        actual_caption = self._sage_reread_role_caption(client, role_entity, code)
        field_name = 'sage_client_code' if role == 'client' else 'sage_vendor_code'
        self.write({field_name: actual_caption or code})
        return actual_caption or code

    @api.model
    def _sage_reread_role_caption(self, client, role_entity, code):
        resources = client.get_all_pages(role_entity, params={'where': self._sage_eq_filter('tiers.code', code)})
        return resources[0].get('Caption') if resources else None

    # ------------------------------------------------------------------
    # Déclencheur manuel (§7.4.1 point 2)
    # ------------------------------------------------------------------

    @api.model
    def _sage_notification(self, message, notification_type):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _("Sage"), 'message': message, 'type': notification_type},
        }

    _SAGE_ROLE_LABEL = {'client': "client", 'vendor': "fournisseur"}
    _SAGE_ROLE_INFO = {
        'client': ('sage_client_code', SAGE_ENTITY_PARTNER_CLIENT),
        'vendor': ('sage_vendor_code', SAGE_ENTITY_PARTNER_VENDOR),
    }

    def action_sage_resolve_client(self):
        return self._sage_resolve_button('client')

    def action_sage_resolve_vendor(self):
        return self._sage_resolve_button('vendor')

    def _sage_resolve_button(self, role):
        """Bouton manuel, un par rôle (§7.4.1 point 2) : lance `_sage_resolve_role` et traduit son
        résultat en toast, ou ouvre le wizard minimaliste (choix parmi plusieurs candidats, ou
        création si rien trouvé).
        """
        self.ensure_one()
        # Le `groups=` posé sur le bouton dans la vue (§7.1) ne fait que le masquer dans l'UI --
        # ça ne bloque pas un appel direct à l'action. Même contrôle qu'exige le wizard (ACL
        # `account.group_account_user`), vérifié ici aussi pour une vraie protection serveur.
        if not self.env.user.has_group('account.group_account_user'):
            raise AccessError(_(
                "Seul un utilisateur du service comptabilité peut rechercher ou créer un code "
                "tiers Sage.",
            ))
        field_name, entity = self._SAGE_ROLE_INFO[role]
        role_label = self._SAGE_ROLE_LABEL[role]
        client = SDataClient(self.env)
        result = self._sage_resolve_role(client, field_name, entity)
        status = result['status']

        if status == 'already_resolved':
            return self._sage_notification(
                _("Ce tiers a déjà un code %(role)s sur Sage.", role=role_label), 'success',
            )
        if status == 'filled':
            return self._sage_notification(_(
                "Code %(role)s trouvé et enregistré sur Sage : %(code)s",
                role=role_label, code=result['code'],
            ), 'success')
        if status == 'differs':
            return self._sage_notification(_(
                "Sage renvoie un code %(role)s différent (%(code)s) -- vérifiez manuellement avant "
                "de changer quoi que ce soit.",
                role=role_label, code=result['code'],
            ), 'warning')

        if status == 'ambiguous':
            candidate_vals = [(0, 0, {
                'code': candidate.get('Caption'),
                'name': candidate.get('raisonSociale') or candidate.get('Caption'),
            }) for candidate in result['candidates']]
            wizard = self.env['sage.partner.resolve.wizard'].create({
                'partner_id': self.id, 'role': role, 'mode': 'choose',
                'candidate_ids': candidate_vals,
            })
        else:  # not_found
            wizard = self.env['sage.partner.resolve.wizard'].create({
                'partner_id': self.id, 'role': role, 'mode': 'create',
                'new_code': self._sage_default_code(role),
            })
        return {
            'type': 'ir.actions.act_window',
            'name': _("Résolution du tiers %(role)s sur Sage", role=role_label),
            'res_model': 'sage.partner.resolve.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
        }
