from odoo.http import request
from odoo.addons.mail.controllers import webmanifest

# Clic sur une notification mobile qui porte une adresse (data.url, voir account.move._expense_notify) : elle s'ouvre
# dans un nouvel onglet, au lieu de l'ouverture « /odoo/<modèle>/<id> » du service worker d'Odoo. Ce code est placé avant
# celui d'Odoo et coupe la suite du traitement du clic (stopImmediatePropagation) ; sans adresse, rien ne change.
#
# Le service worker d'Odoo ne prend jamais la main seul : une nouvelle version reste « en attente » tant qu'un onglet
# d'Odoo est ouvert, et le téléphone garde alors l'ancien comportement. skipWaiting et clients.claim la font passer
# aussitôt, dès le prochain chargement d'Odoo.
EXPENSE_SERVICE_WORKER = """
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("notificationclick", (event) => {
    const url = event.notification.data && event.notification.data.url;
    if (!url) {
        return;
    }
    event.notification.close();
    event.stopImmediatePropagation();
    event.waitUntil(self.clients.openWindow(url));
});
"""


class WebManifest(webmanifest.WebManifest):

    def _get_service_worker_content(self):
        body = super()._get_service_worker_content()
        if request.env.user._is_internal():
            body = EXPENSE_SERVICE_WORKER + body
        return body
