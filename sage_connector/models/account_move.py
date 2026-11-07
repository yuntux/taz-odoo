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

# TODO : aucun code de type de pièce pour la vente n'est documenté officiellement (seuls "AF"/"FF"
# apparaissent dans l'exemple officiel, pour des pièces d'achat, sans qu'on puisse distinguer
# lequel correspond à une facture ou à un avoir). Hypothèses de travail à confirmer avec Mercuria.
SAGE_DOCUMENT_TYPE_BY_MOVE_TYPE = {
    'out_invoice': 'VF',
    'out_refund': 'VA',
    'in_invoice': 'AF',
    'in_refund': 'FF',
}

SAGE_PARTNER_CODE_FIELD_BY_MOVE_TYPE = {
    'out_invoice': 'sage_client_code',
    'out_refund': 'sage_client_code',
    'in_invoice': 'sage_vendor_code',
    'in_refund': 'sage_vendor_code',
}

_SAGE_SUCCESS_MESSAGE_RE = re.compile(r"Enregistrement pièce n°\s*(\S+)")


class AccountMove(models.Model):
    _inherit = 'account.move'

    sage_sync_status = fields.Selection([
        ('to_send', 'À envoyer'),
        ('sent', 'Envoyé'),
        ('to_update', 'À mettre à jour'),
    ], copy=False, readonly=True)

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
        partner._sage_search_cascade_auto()
        if not partner[field_name]:
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

        in_scope = self.filtered(lambda m: m._sage_is_in_scope())
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

    def unlink(self):
        blocked = self.filtered(lambda m: m.sage_sync_status in ('sent', 'to_update'))
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

    def _sage_build_journal_entry_lines(self, session_uuid, start_index):
        """Construit les opérations `$batch` (une par account.move.line au solde non nul) pour
        cette pièce, prêtes à être insérées dans le lot partagé (§6.1 point 3).
        """
        self.ensure_one()
        company = self.company_id
        common = {
            'codeJournal': self.journal_id.sage_journal_code,
            'numeroPiece': self.name,
            'datePiece': str(self.date or ''),
            'dateEcheance': str(self.invoice_date_due or self.date or ''),
            'deviseEcriture': self.currency_id.name,
            'deviseTenueCompte': company.currency_id.name,
            'etablissement': company.sage_etablissement_code,
            'typeEcriture': SAGE_JOURNAL_ENTRY_TYPE,
            'typePiece': SAGE_DOCUMENT_TYPE_BY_MOVE_TYPE.get(self.move_type),
            'urlScanPiece': "%s/mail/view?model=account.move&res_id=%s" % (self.get_base_url(), self.id),
        }
        partner_code_field = SAGE_PARTNER_CODE_FIELD_BY_MOVE_TYPE.get(self.move_type)

        operations = []
        index = start_index
        for line in self.line_ids.filtered('balance'):
            entry = dict(common)
            entry.update({
                'compteGeneral': line.account_id.code,
                'sens': 'D' if line.balance > 0 else 'C',
                'montant': abs(line.balance),
                'montantEnDevise': abs(line.balance),
                'libelleEcriture': line.name or self.ref or self.name,
            })
            code = line.partner_id[partner_code_field] if line.partner_id and partner_code_field else False
            if code:
                entry['compteAuxiliaire'] = code
                entry['typeCompte'] = 'X'
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
        moves = self.search([('sage_sync_status', '=', 'to_send')])
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

        operations = [session_operation]
        index = 0
        for move in moves:
            move_operations = move._sage_build_journal_entry_lines(session_uuid, index)
            operations.extend(move_operations)
            index += len(move_operations)

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

        moves._sage_apply_batch_result(result)

    def _sage_apply_batch_result(self, result):
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
                continue
            error_messages = [m for m in messages if move.name and move.name in m]
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
