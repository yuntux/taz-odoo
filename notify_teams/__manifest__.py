{
    'name': "notify_teams",
    'summary': """Microsoft Teams comme troisième canal de notification d'Odoo, avec un bouton d'action propre à chaque type""",
    'description': """
Ajoute « Gérer dans Microsoft Teams » aux préférences de notification (à côté de « Gérer par e-mails » et
« Gérer dans Odoo »). Une notification porte un titre, un texte et une adresse à ouvrir, avec un libellé de bouton
qui dépend de son type. L'envoi passe par une file d'attente (déclenchée à la demande, reprise toutes les 10 minutes)
puis par un transport remplaçable (flux Power Automate, Logic App ou webhook Teams).
Cahier des charges : SPECIFICATION.md. Module au squelette seulement : aucun modèle n'est encore implémenté.
    """,
    'author': "Aurélien Dumaine",
    'website': "https://www.dumaine.me",
    'license': 'LGPL-3',
    'category': 'Discuss',
    'version': "18.0.0.1.0",
    'depends': ['mail'],
    'data': [
        'security/ir.model.access.csv',
    ],
}
