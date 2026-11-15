import hashlib
import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class QontoWebhook(http.Controller):

    @staticmethod
    def _signature_matches(secret, header, body):
        """En-tête X-Qonto-Signature : « t=<horodatage>,v1=<HMAC-SHA256 de horodatage.corps> »."""
        parts = dict(p.split('=', 1) for p in (header or '').split(',') if '=' in p)
        if not (secret and parts.get('t') and parts.get('v1')):
            return False
        expected = hmac.new(secret.encode(), ('%s.' % parts['t']).encode() + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, parts['v1'])

    @http.route('/expense_qonto/webhook', type='http', auth='public', methods=['POST'], csrf=False)
    def webhook(self):
        body = request.httprequest.get_data()
        header = request.httprequest.headers.get('X-Qonto-Signature')
        companies = request.env['res.company'].sudo().search([('qonto_webhook_secret', '!=', False)])
        company = companies.filtered(lambda c: self._signature_matches(c.qonto_webhook_secret, header, body))[:1]
        if not company:
            return request.make_response('invalid signature', status=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return request.make_response('invalid body', status=400)
        data = payload.get('data') or payload
        tx = data.get('transaction') if isinstance(data.get('transaction'), dict) else data
        if not tx.get('card_id') or 'amount' not in tx:
            # Charge réduite : on relit la transaction complète par l'API.
            transaction_id = tx.get('transaction_id') or tx.get('id')
            if not transaction_id:
                return request.make_response('ignored', status=200)
            tx = request.env['qonto.api'].sudo().get_transaction(company, transaction_id)
        request.env['account.move'].sudo()._qonto_create_expense(company, tx)
        return request.make_response('ok', status=200)

    @http.route('/expense_qonto/oauth/callback', type='http', auth='user', methods=['GET'])
    def oauth_callback(self, code=None, state=None, error=None, **kwargs):
        """Retour de Qonto après l'autorisation (OAuth 2.0) : réservé à l'administrateur qui a lancé la connexion, dont
        c'est le navigateur que Qonto ramène ici. Revient ensuite sur l'écran des paramètres."""
        if not request.env.user.has_group('base.group_system'):
            return request.make_response('forbidden', status=403)
        if error or not (code and state):
            _logger.warning("Qonto OAuth : autorisation refusée ou incomplète (%s).", error)
            return request.redirect('/odoo/settings')
        try:
            company_id = int((state or '').split('-', 1)[0])
        except ValueError:
            return request.make_response('invalid state', status=400)
        company = request.env['res.company'].sudo().browse(company_id).exists()
        if not company:
            return request.make_response('invalid state', status=400)
        try:
            company._qonto_oauth_finish(code, state)
        except Exception:  # noqa: BLE001 : le détail est dans le journal, l'écran des paramètres reste atteignable
            _logger.exception("Qonto OAuth : échange du code impossible pour la société %s.", company.name)
        return request.redirect('/odoo/settings')
