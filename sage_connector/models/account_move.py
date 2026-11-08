import json
import logging
import re
import traceback
import uuid as uuid_module
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .sage_client import (
    SAGE_ENTITY_JOURNAL_IMPORT_ENTRY,
    SAGE_ENTITY_JOURNAL_IMPORT_EXECUTE,
    SAGE_ENTITY_JOURNAL_IMPORT_SESSION,
    SDataClient,
)

_logger = logging.getLogger(__name__)

SAGE_JOURNAL_ENTRY_TYPE = "Brouillard"

# "VA" (avoir client) reste une hypothèse non confirmée (par symétrie avec FC). Côté fournisseur,
# "FF" = Factures fournisseurs, "AF" = Avoir fournisseurs (liste IHM Sage, SPEC.md §7.2) -- plusieurs
# autres types fournisseur existent (FFM, FFS, FFTVA...) mais leur critère de choix n'est pas encore
# identifié, cf. TODO dans SPEC.md §7.2.
SAGE_DOCUMENT_TYPE_BY_MOVE_TYPE = {
    'out_invoice': 'FC',
    'out_refund': 'VA',
    'in_invoice': 'FF',
    'in_refund': 'AF',
    # Écriture diverse ("entry") -- la portée (`_sage_is_in_scope`) couvre bien ce type, mais ce
    # mapping ne l'avait pas, ce qui envoyait un `typePiece: null` à Sage (SPEC.md §7.2 : "OD").
    'entry': 'OD',
}

# Pièces intragroupe (le tiers est lui-même une société du groupe, identifié par
# `res.company.partner_id`) : type de pièce distinct. TODO : équivalent non confirmé pour un avoir
# client intragroupe, ni pour un avoir fournisseur intragroupe (FFI, ou AF comme pour un avoir
# fournisseur normal ? -- à trancher avec Mercuria/MC2I, cf. SPEC.md §7.2).
SAGE_INTRAGROUP_DOCUMENT_TYPE_BY_MOVE_TYPE = {
    'out_invoice': 'FCI',
    'in_invoice': 'FFI',
}

SAGE_PARTNER_CODE_FIELD_BY_MOVE_TYPE = {
    'out_invoice': 'sage_client_code',
    'out_refund': 'sage_client_code',
    'in_invoice': 'sage_vendor_code',
    'in_refund': 'sage_vendor_code',
}

_SAGE_SUCCESS_MESSAGE_RE = re.compile(r"Enregistrement pièce n°\s*(\S+)")
_SAGE_LOCATION_OID_RE = re.compile(r"\('([^']+)'\)\s*$")

# Limite confirmée empiriquement sur le bac à sable (bissection le 2026-11-08) : un `libelleEcriture`
# de plus de 128 caractères fait échouer l'EXEC avec un message générique "(TranBatch) Erreur de
# transaction :1", sans rapport avec le vrai contenu -- aucune limite documentée officiellement.
SAGE_LIBELLE_MAX_LENGTH = 128


class AccountMove(models.Model):
    _inherit = 'account.move'

    sage_sync_status = fields.Selection([
        ('to_send', 'À envoyer'),
        ('sent', 'Envoyé'),
        ('to_update', 'À mettre à jour'),
    ], copy=False, readonly=True)
    # OID Sage (`TImportEcritures`) de chaque ligne envoyée, dans l'ordre d'envoi -- utilisé pour
    # retenter un `PUT` ciblé (mise à jour) sur une pièce `to_update` plutôt qu'un nouveau `POST`
    # (hypothèse non confirmée en bac à sable, cf. SPEC.md §6.1 point 4). JSON, pas de vue associée
    # (champ technique).
    sage_import_line_oids = fields.Text(copy=False)

    # ------------------------------------------------------------------
    # Portée (SPEC.md §6.1 point 0)
    # ------------------------------------------------------------------

    def _sage_is_in_scope(self):
        """La portée ne dépend que du journal et de la date (§6.1 point 0) -- elle couvre tout
        type de pièce (y compris une écriture diverse `entry`), pas seulement les factures/avoirs.
        Le `move_type` n'intervient que pour savoir QUEL code tiers est requis (cf.
        `_sage_ensure_partner_code`), pas pour décider si la pièce est concernée ou non.
        """
        self.ensure_one()
        journal = self.journal_id
        if not journal.sage_sync_date_start:
            return False
        move_date = self.date
        if not move_date or move_date <= journal.sage_sync_date_start:
            return False
        if journal.sage_sync_date_end and move_date >= journal.sage_sync_date_end:
            return False
        return True

    # ------------------------------------------------------------------
    # Validation (§6.1)
    # ------------------------------------------------------------------

    def _sage_ensure_partner_code(self):
        """Contrôle bloquant (§6.1 point 1) : tente d'abord la recherche automatique silencieuse
        (§8.1 critères exacts uniquement) avant de bloquer la validation.
        """
        self.ensure_one()
        field_name = SAGE_PARTNER_CODE_FIELD_BY_MOVE_TYPE.get(self.move_type)
        if not field_name:
            return
        partner = self.partner_id
        if partner[field_name]:
            return
        blocked = partner._sage_search_cascade_auto()
        if partner[field_name]:
            return
        if blocked:
            # Sage a bien renvoyé un code, mais l'écriture sur la fiche a été refusée (fiche
            # protégée -- déjà facturée -- et utilisateur courant non-ADV, cf.
            # `project_accounting`). Un message "aucun code trouvé" serait trompeur ici.
            raise UserError(_(
                "Un code Sage a été trouvé pour %(partner)s mais n'a pas pu être enregistré car sa "
                "fiche est protégée (déjà facturée) : seul un utilisateur du service comptabilité "
                "peut l'enregistrer. Demandez-lui d'ouvrir la fiche et d'utiliser le bouton "
                "« Rechercher / créer sur Sage ».",
                partner=partner.display_name,
            ))
        raise UserError(_(
            "Aucun code Sage trouvé pour le tiers %(partner)s. Ouvrez sa fiche contact et "
            "utilisez le bouton « Rechercher / créer sur Sage » avant de valider cette pièce.",
            partner=partner.display_name,
        ))

    def action_post(self):
        in_scope = self.filtered(lambda m: m._sage_is_in_scope())
        for move in in_scope:
            move._sage_ensure_partner_code()

        res = super().action_post()

        for move in in_scope:
            if not move.sage_sync_status:
                move.sage_sync_status = 'to_send'
            elif move.sage_sync_status == 'sent':
                move.sage_sync_status = 'to_update'
        if in_scope:
            self.env['account.move']._sage_process_pending_batch()
        return res

    # ------------------------------------------------------------------
    # Suppression (§6.2)
    # ------------------------------------------------------------------

    def _sage_is_locked(self):
        """Pièce déjà envoyée (ou en attente de mise à jour) sur Sage -- ne doit plus pouvoir être
        supprimée, ni directement ni via une de ses lignes (cf. `account.move.line.unlink`, qui
        réutilise ce même prédicat pour ne pas dupliquer la règle de blocage en deux versions
        indépendantes).
        """
        self.ensure_one()
        return self.sage_sync_status in ('sent', 'to_update')

    def unlink(self):
        blocked = self.filtered(lambda m: m._sage_is_locked())
        if blocked:
            raise UserError(_(
                "Ces pièces ont déjà été envoyées à Sage et ne peuvent plus être supprimées : "
                "%(names)s. Il faut les contre-passer plutôt que les supprimer.",
                names=", ".join(blocked.mapped('name')),
            ))
        return super().unlink()

    # ------------------------------------------------------------------
    # Mapping écriture (§7)
    # ------------------------------------------------------------------

    @api.model
    def _sage_intragroup_partner_ids(self):
        """Partenaires de toutes les sociétés du groupe -- précalculé une fois pour tout un lot
        (cf. `_sage_process_pending_batch`) au lieu d'une requête SQL par pièce dans
        `_sage_is_intragroup_partner`.
        """
        return set(self.env['res.company'].sudo().search([]).mapped('partner_id').ids)

    def _sage_is_intragroup_partner(self, intragroup_partner_ids=None):
        """Le tiers facturé est-il lui-même une société du groupe (identifiée par
        `res.company.partner_id`) ? Détermine le type de pièce FC/FCI (§7).
        """
        self.ensure_one()
        if intragroup_partner_ids is None:
            intragroup_partner_ids = self._sage_intragroup_partner_ids()
        commercial_partner_id = self.partner_id.commercial_partner_id.id
        return (
            commercial_partner_id in intragroup_partner_ids
            and commercial_partner_id != self.company_id.partner_id.id
        )

    def _sage_document_type(self, intragroup_partner_ids=None):
        self.ensure_one()
        is_intragroup = (
            self.move_type in SAGE_INTRAGROUP_DOCUMENT_TYPE_BY_MOVE_TYPE
            and self._sage_is_intragroup_partner(intragroup_partner_ids)
        )
        if is_intragroup:
            return SAGE_INTRAGROUP_DOCUMENT_TYPE_BY_MOVE_TYPE[self.move_type]
        return SAGE_DOCUMENT_TYPE_BY_MOVE_TYPE.get(self.move_type)

    @staticmethod
    def _sage_datetime(value):
        """Sage attend un datetime ISO-8601 complet (`...T00:00:00.000Z`), pas une simple date --
        format confirmé empiriquement (SPEC.md §7.4) sur un exemple fonctionnel.
        """
        if not value:
            return ''
        return "%sT00:00:00.000Z" % value.isoformat()

    def _sage_build_journal_entry_lines(self, session_uuid, start_index, intragroup_partner_ids=None):
        """Construit les opérations `$batch` (une par account.move.line au solde non nul) pour
        cette pièce, prêtes à être insérées dans le lot partagé (§6.1 point 3).

        Pièce déjà envoyée une première fois (`sage_import_line_oids` renseigné, cas `to_update`) :
        **hypothèse non confirmée en bac à sable** (SPEC.md §6.1 point 4) -- on envoie un `PUT` sur
        l'OID `TImportEcritures` déjà connu pour cette ligne au lieu d'un nouveau `POST`, pour
        mettre à jour l'écriture déjà importée plutôt que d'en recréer une (et donc la dupliquer).
        Si le nombre de lignes a changé depuis le premier envoi (ligne ajoutée/supprimée), les
        lignes au-delà de celles déjà connues repartent en `POST` classique.
        """
        self.ensure_one()
        stored_oids = json.loads(self.sage_import_line_oids) if self.sage_import_line_oids else []
        company = self.company_id
        common = {
            'codeJournal': self.journal_id.sage_journal_code,
            'numeroPiece': self.name,
            'datePiece': self._sage_datetime(self.date),
            'dateEcheance': self._sage_datetime(self.invoice_date_due or self.date),
            'deviseEcriture': self.currency_id.name,
            'deviseTenueCompte': company.currency_id.name,
            'etablissement': company.sage_etablissement_code,
            'typeEcriture': SAGE_JOURNAL_ENTRY_TYPE,
            'typePiece': self._sage_document_type(intragroup_partner_ids),
            'urlScanPiece': "%s/mail/view?model=account.move&res_id=%s" % (self.get_base_url(), self.id),
        }
        partner_code_field = SAGE_PARTNER_CODE_FIELD_BY_MOVE_TYPE.get(self.move_type)

        operations = []
        index = start_index
        for line_position, line in enumerate(self.line_ids.filtered('balance')):
            entry = dict(common)
            entry.update({
                'compteGeneral': line.account_id.code,
                'sens': 'D' if line.balance > 0 else 'C',
                'montant': abs(line.balance),
                'montantEnDevise': abs(line.balance),
                'libelleEcriture': (line.name or self.ref or self.name or '')[:SAGE_LIBELLE_MAX_LENGTH],
            })
            is_collectif_line = line.account_id.account_type in ('asset_receivable', 'liability_payable')
            entry['typeCompte'] = 'X' if is_collectif_line else 'G'
            if is_collectif_line:
                code = line.partner_id[partner_code_field] if line.partner_id and partner_code_field else False
                if code:
                    entry['compteAuxiliaire'] = code
            existing_oid = stored_oids[line_position] if line_position < len(stored_oids) else None
            if existing_oid:
                entry.update({
                    '$httpMethod': 'PUT',
                    '$url': "%s('%s')" % (SAGE_ENTITY_JOURNAL_IMPORT_ENTRY, existing_oid),
                    'SessionImport': {'$uuid': session_uuid},
                })
            else:
                entry.update({
                    '$httpMethod': 'POST',
                    '$url': SAGE_ENTITY_JOURNAL_IMPORT_ENTRY,
                    '$uuid': 'line-%s' % index,
                    'SessionImport': {'$uuid': session_uuid},
                })
            operations.append(entry)
            index += 1
        return operations

    # ------------------------------------------------------------------
    # Envoi groupé (§6.1 point 3)
    # ------------------------------------------------------------------

    @api.model
    def _sage_process_pending_batch(self):
        # Filtré sur la société courante : un seul `SDataClient` (lié à `env.company`) est
        # construit pour tout le lot, il ne doit pas mélanger les pièces d'autres sociétés qui
        # auraient chacune leur propre contrat/endpoint Sage.
        # `to_update` est repris ici (hypothèse non confirmée en bac à sable, cf. SPEC.md §6.1
        # point 4) : `_sage_build_journal_entry_lines` envoie un `PUT` sur l'OID déjà connu pour
        # chaque ligne au lieu d'un nouveau `POST`, pour mettre à jour l'écriture plutôt que la
        # dupliquer -- à vérifier en bac à sable avant mise en prod.
        moves = self.search([
            ('sage_sync_status', 'in', ('to_send', 'to_update')),
            ('company_id', '=', self.env.company.id),
        ])
        if not moves:
            return
        try:
            client = SDataClient(self.env)
        except Exception:
            _logger.warning("Sage connector: unable to build API client, skipping this pass")
            _logger.warning(traceback.format_exc())
            return

        session_uuid = 'session'
        now = datetime.now()
        session_caption = "%s%03d-%s" % (
            now.strftime('%Y%m%d%H%M%S'), now.microsecond // 1000, uuid_module.uuid4().hex,
        )
        session_operation = {
            '$httpMethod': 'POST',
            '$url': SAGE_ENTITY_JOURNAL_IMPORT_SESSION,
            '$uuid': session_uuid,
            'Caption': session_caption,
        }
        # Volontairement non renseigné par défaut (cf. SPEC.md §4.2/§7.1) : les lignes envoyées
        # sont toujours équilibrées par construction, un compte d'équilibre masquerait un bug de
        # mapping au lieu de le faire échouer.
        compte_equilibre = self.env.company.sage_compte_equilibre
        if compte_equilibre:
            session_operation['CompteEquilibre'] = {
                '$url': "TCompteGeneral(codeCompte eq '%s')" % compte_equilibre,
            }

        intragroup_partner_ids = self._sage_intragroup_partner_ids()
        operations = [session_operation]
        index = 0
        valid_moves = self.env['account.move']
        move_line_ranges = {}
        for move in moves:
            try:
                move_operations = move._sage_build_journal_entry_lines(
                    session_uuid, index, intragroup_partner_ids,
                )
            except Exception:
                # Une pièce malformée ne doit pas bloquer tout le lot (le cron de rattrapage
                # retentera cette pièce seule au prochain passage, les autres n'ont pas à attendre).
                _logger.warning("Sage connector: unable to build lines for piece %s", move.name)
                _logger.warning(traceback.format_exc())
                continue
            operations.extend(move_operations)
            move_line_ranges[move.id] = (index, len(move_operations))
            index += len(move_operations)
            valid_moves |= move
        moves = valid_moves
        if not moves:
            return

        operations.append({
            '$': {'$transaction': ['commit']},
            '$httpMethod': 'EXEC',
            '$url': SAGE_ENTITY_JOURNAL_IMPORT_EXECUTE,
            '$uuid': 'processus',
            '$resource': {
                'codeSessionInf': session_caption,
                'codeSessionSup': session_caption,
                'importerExterne': True,
            },
        })

        try:
            result = client.batch(operations)
        except Exception:
            _logger.warning("Sage connector: batch send failed, pieces remain to_send")
            _logger.warning(traceback.format_exc())
            return

        # L'opération EXEC ci-dessus porte son propre `$transaction: ['commit']` -- Sage l'a donc
        # déjà appliqué de façon définitive à cet instant, indépendamment de la transaction Odoo en
        # cours. Committer immédiatement l'écriture `sage_sync_status` qui suit (dans
        # `_sage_apply_batch_result`) pour qu'un rollback Odoo ultérieur (plus haut dans l'appelant,
        # ex. `action_post`) ne puisse jamais faire "oublier" un envoi déjà irréversible côté Sage
        # et le renvoyer en double au prochain passage.
        moves._sage_apply_batch_result(result, move_line_ranges)
        if self._can_commit():
            self.env.cr.commit()

    def _sage_store_import_line_oids(self, resources, line_range):
        """Mémorise l'OID `TImportEcritures` de chaque ligne envoyée (POST) ou mise à jour (PUT),
        pour permettre un `PUT` ciblé la prochaine fois que cette pièce sera corrigée (`to_update`)
        -- cf. `_sage_build_journal_entry_lines`. Un `PUT` ne renvoie vraisemblablement pas de
        `$location` (ressource déjà existante, pas nouvellement créée) : dans ce cas on garde l'OID
        déjà connu pour cette position plutôt que de le perdre.
        """
        self.ensure_one()
        start_index, count = line_range
        line_resources = resources[1 + start_index:1 + start_index + count]
        previous_oids = json.loads(self.sage_import_line_oids) if self.sage_import_line_oids else []
        oids = []
        for position, resource in enumerate(line_resources):
            match = _SAGE_LOCATION_OID_RE.search(resource.get('$location') or '')
            if match:
                oids.append(match.group(1))
            elif position < len(previous_oids):
                oids.append(previous_oids[position])
            else:
                oids.append(None)
        self.sage_import_line_oids = json.dumps(oids)

    def _sage_apply_batch_result(self, result, move_line_ranges=None):
        """Lecture pièce par pièce du résultat (§6.1 point 3, §7.4) : un `$httpStatus` 200 sur
        l'exécution ne garantit pas que chaque pièce a été acceptée -- il faut chercher, pour
        chaque pièce, un message de succès explicite avant de la marquer `sent`.
        """
        resources = result.get('$resources', []) if isinstance(result, dict) else []
        exec_resource = next((r for r in resources if r.get('$httpMethod') == 'EXEC'), None)
        messages = (exec_resource or {}).get('$messages') or []

        success_numeros = set()
        for message in messages:
            match = _SAGE_SUCCESS_MESSAGE_RE.search(message)
            if match:
                success_numeros.add(match.group(1))

        for move in self:
            if move.name and move.name in success_numeros:
                move.sage_sync_status = 'sent'
                if move_line_ranges and move.id in move_line_ranges:
                    move._sage_store_import_line_oids(resources, move_line_ranges[move.id])
                continue
            # Comparaison exacte (pas une simple sous-chaîne) : un numéro de pièce préfixe d'un
            # autre dans le même lot (ex. "FAC/2024/0001" vs "FAC/2024/00011") ne doit pas voir le
            # message de l'un attribué par erreur à l'autre dans les logs.
            name_pattern = re.compile(r'(?<!\d)%s(?!\d)' % re.escape(move.name)) if move.name else None
            error_messages = [m for m in messages if name_pattern and name_pattern.search(m)]
            if error_messages:
                _logger.warning(
                    "Sage connector: piece %s rejected: %s", move.name, "; ".join(error_messages),
                )
            else:
                _logger.warning(
                    "Sage connector: piece %s absent from batch response, status undetermined",
                    move.name,
                )
            # Reste 'to_send' dans les deux cas : retenté au prochain lot (validation suivante ou
            # cron de rattrapage).
