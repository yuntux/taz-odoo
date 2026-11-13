{
    'name': "expense_qonto",
    'summary': """Dépenses par carte Qonto : une transaction par carte crée la dépense du porteur""",
    'description': """
Lot 3 du cadrage « Gestion des dépenses ».

Qonto appelle le webhook ``/expense_qonto/webhook`` à chaque transaction par carte ; une tâche planifiée relit
les transactions récentes pour rattraper les appels perdus. Chaque transaction par carte réglée crée une
dépense en brouillon (origine « carte ») pour le porteur de la carte, déjà payée sur le journal Qonto :
le porteur dépose ensuite son justificatif, choisit le projet, puis la dépense suit le bon à payer.

Les identifiants de l'API sont portés par la société (mêmes noms que le module Akretion
account_statement_import_api) et la carte reprend le modèle ``account.bank.statement.card`` d'Akretion,
sans tiers « divers ».
    """,
    'author': "Aurélien Dumaine",
    'website': "https://www.dumaine.me",
    'license': 'LGPL-3',
    'category': 'Accounting',
    'version': "18.0.1.0.0",
    'depends': ['expense_core'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'views/account_bank_statement_card_views.xml',
        'views/res_config_settings_views.xml',
        'views/account_journal_views.xml',
    ],
}
