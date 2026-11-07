{
    'name': "sage_connector",

    'summary': """Export des écritures comptables et synchronisation des tiers vers Sage FRP 1000""",

    'description': """
Export vers Sage FRP 1000 (API SData) des écritures comptables des journaux activés, à la
validation des factures/avoirs. Recherche et création des tiers comptables correspondants
(SIRET, TVA intracommunautaire, raison sociale / nom+prénom).

Voir SPEC.md à la racine du module pour le détail fonctionnel et les points encore ouverts.
    """,

    'author': "Aurélien Dumaine",
    'website': "https://www.dumaine.me",
    'license': 'LGPL-3',

    'category': 'Accounting',
    'version': "18.0.1.0.0",

    'depends': ['account', 'l10n_fr_siret', 'taz-common', 'project_accounting'],

    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'views/res_config_settings_views.xml',
        'views/account_journal_views.xml',
        'views/account_move_views.xml',
        'views/res_partner_views.xml',
        'wizards/sage_partner_resolve_wizard_views.xml',
    ],
}
