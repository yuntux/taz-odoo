from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    sage_api_endpoint = fields.Char(
        string="Sage - URL de base de l'API",
        help="Ex: https://mc2i-compta-prd.em.cloud-by-sage.fr",
    )
    sage_api_token = fields.Char(
        string="Sage - Token Bearer",
        help="Peut être amené à changer côté Sage (rotation du token).",
    )
    sage_contract_code = fields.Char(
        string="Sage - Code contrat SData",
        help="Segment de contrat dans l'URL SData (ex: MC2I). Ne pas y ajouter de dataset : "
             "chaque entité appelée par le connecteur porte déjà le sien dans son propre chemin.",
    )
    sage_etablissement_code = fields.Char(
        string="Sage - Code établissement",
        help="Valeur du champ 'etablissement', obligatoire sur chaque ligne d'écriture importée "
             "(ex. observé dans la documentation officielle : 'ETS1-S01').",
    )
    sage_compte_equilibre = fields.Char(
        string="Sage - Compte général d'équilibre",
        help="Compte utilisé comme 'CompteEquilibre' à la création d'une session d'import. "
             "Laissé vide par défaut : les écritures envoyées sont toujours équilibrées par "
             "construction, et on préfère qu'un déséquilibre fasse échouer l'import plutôt que "
             "d'être absorbé silencieusement par un compte de secours.",
    )
