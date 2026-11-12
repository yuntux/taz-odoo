{
    'name': "expense_core",

    'summary': """Bon à payer (BAP) à deux validations sur les factures et reçus fournisseurs""",

    'description': """
Lot 1 du cadrage « Gestion des dépenses » (rubrique 17.3 du document).

Le circuit de bon à payer s'appuie sur base_tier_validation (OCA, server-ux) : première validation par
l'Office (groupe TAZ-ADV), seconde par le directeur de mission des projets de la pièce ; une pièce sans
projet n'a que la première. L'écriture comptable ne peut être validée qu'une fois la pièce approuvée.
Les deux niveaux sont livrés inactifs : les activer (Paramètres > Validation par niveaux) met le circuit
en service pour la société concernée.

Le paiement passe par les ordres de paiement OCA (account_payment_batch_oca, SEPA) : seules les pièces
comptabilisées, donc approuvées, y entrent. Un réglage de société, « Mode de paiement des dépenses »,
s'applique aux fournisseurs qui n'ont pas de mode de paiement préféré.
    """,

    'author': "Aurélien Dumaine",
    'website': "https://www.dumaine.me",
    'license': 'LGPL-3',

    'category': 'Accounting',
    'version': "18.0.2.0.0",

    'depends': [
        'account', 'hr', 'mail', 'base_tier_validation', 'taz-common', 'project_accounting', 'staffing',
        'account_payment_batch_oca', 'account_payment_sepa_credit_transfer',
    ],

    'data': [
        'security/ir.model.access.csv',
        'security/expense_security.xml',
        'data/tier_definition_data.xml',
        'data/ir_cron_data.xml',
        'views/account_move_views.xml',
        'views/expense_menus.xml',
        'views/product_views.xml',
        'views/expense_receipt_views.xml',
        'views/res_config_settings_views.xml',
    ],
}
