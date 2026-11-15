from odoo import api, models


class MailThread(models.AbstractModel):
    """Plus d'abonnement automatique à une fiche, pour tous les modèles.

    Par défaut Odoo abonne automatiquement :
    - le responsable d'une fiche (champ user_id, user_ids...) dès qu'il est posé, et lui envoie « Vous avez été assigné à… » ;
    - celui qui crée la fiche.
    Un abonné reçoit ensuite tout ce qui s'écrit sur la fiche : sur cette instance, ces abonnements automatiques ont
    produit des dizaines de milliers de mails (une affectation en masse de responsables sur des contacts en a fait plus de
    13 000) et seraient autant de notifications push. Un utilisateur s'abonne désormais lui-même, ou est abonné à la main.

    Restent inchangés : l'abonnement de l'auteur d'un message écrit à la main, l'abonnement en cascade d'un parent à ses
    fiches (suivre un projet abonne à ses tâches) et les abonnements posés explicitement (message_subscribe).
    """
    _inherit = 'mail.thread'

    @api.model_create_multi
    def create(self, vals_list):
        # mail_create_nosubscribe : le créateur n'est pas abonné à la fiche qu'il crée. Les fiches créées sont rendues avec
        # l'environnement d'origine : sinon elles garderaient ce contexte et l'auteur d'un message écrit à la main juste
        # après ne serait plus abonné non plus.
        records = super(MailThread, self.with_context(mail_create_nosubscribe=True)).create(vals_list)
        return records.with_env(self.env)

    def _message_auto_subscribe(self, updated_values, followers_existing_policy='skip'):
        # Les champs qui désignent un utilisateur (responsable, commercial, assignés...) ne déclenchent plus ni abonnement
        # ni message d'assignation. Ils sont retirés ici, au point d'appel commun à tous les modèles, et non dans
        # _message_auto_subscribe_followers, que certains modèles (tâches, par exemple) remplacent sans appeler leur parent.
        responsible_fields = {
            name for name, field in self._fields.items()
            if field.type in ('many2one', 'many2many', 'one2many') and field.comodel_name == 'res.users'}
        updated_values = {key: value for key, value in updated_values.items() if key not in responsible_fields}
        return super()._message_auto_subscribe(updated_values, followers_existing_policy=followers_existing_policy)
