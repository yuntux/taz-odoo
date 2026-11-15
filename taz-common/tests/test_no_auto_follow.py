from odoo import Command
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestNoAutoFollow(TransactionCase):
    """Plus d'abonnement automatique à une fiche (responsable, créateur), pour tous les modèles."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ref = cls.env.ref
        # Le créateur peut créer des contacts et des projets ; le responsable est un utilisateur ordinaire.
        cls.creator = cls._user('createur_nofollow', [
            ref('base.group_user'), ref('base.group_partner_manager'), ref('project.group_project_manager')])
        cls.responsible = cls._user('responsable_nofollow', [ref('base.group_user')])

    @classmethod
    def _user(cls, login, groups):
        return cls.env['res.users'].with_context(no_reset_password=True).create({
            'name': login, 'login': login, 'email': f'{login}@example.com',
            'groups_id': [Command.set([group.id for group in groups])]})

    def _followers(self, record):
        # Lu dans la table des abonnés, pas dans le champ calculé message_partner_ids, dont le cache peut être périmé.
        return self.env['mail.followers'].sudo().search([
            ('res_model', '=', record._name), ('res_id', '=', record.id)]).partner_id

    def _assignment_notifications(self, record, user):
        return self.env['mail.notification'].sudo().search([
            ('res_partner_id', '=', user.partner_id.id), ('mail_message_id.model', '=', record._name),
            ('mail_message_id.res_id', '=', record.id)])

    def test_the_responsible_is_not_subscribed_nor_notified_at_creation(self):
        partner = self.env['res.partner'].with_user(self.creator).sudo(False).create(
            {'name': 'Fiche sans abonnement', 'user_id': self.responsible.id})
        self.assertNotIn(self.responsible.partner_id, self._followers(partner))
        self.assertFalse(self._assignment_notifications(partner, self.responsible))

    def test_the_responsible_is_not_subscribed_nor_notified_when_changed(self):
        partner = self.env['res.partner'].create({'name': 'Fiche modifiée'})
        partner.with_user(self.creator).write({'user_id': self.responsible.id})
        self.assertNotIn(self.responsible.partner_id, self._followers(partner))
        self.assertFalse(self._assignment_notifications(partner, self.responsible))

    def test_the_creator_is_not_subscribed(self):
        partner = self.env['res.partner'].with_user(self.creator).create({'name': 'Fiche du créateur'})
        self.assertNotIn(self.creator.partner_id, self._followers(partner))

    def test_it_holds_for_other_models_and_other_responsible_fields(self):
        project = self.env['project.project'].with_user(self.creator).create(
            {'name': 'Projet sans abonnement', 'user_id': self.responsible.id})
        self.assertNotIn(self.responsible.partner_id, self._followers(project))
        self.assertNotIn(self.creator.partner_id, self._followers(project))
        task = self.env['project.task'].sudo().create(
            {'name': 'Tâche', 'project_id': project.id, 'user_ids': [Command.set(self.responsible.ids)]})
        self.assertNotIn(self.responsible.partner_id, self._followers(task))

    def test_an_explicit_subscription_still_works(self):
        partner = self.env['res.partner'].create({'name': 'Fiche suivie à la main'})
        partner.message_subscribe(partner_ids=self.responsible.partner_id.ids)
        self.assertIn(self.responsible.partner_id, self._followers(partner))

    def test_the_author_of_a_manual_message_is_still_subscribed(self):
        partner = self.env['res.partner'].create({'name': 'Fiche commentée'})
        partner.with_user(self.creator).message_post(body='Un commentaire', message_type='comment')
        self.assertIn(self.creator.partner_id, self._followers(partner))
