# notify_teams — Cahier des charges

Module Odoo 18 Community (on-premise). Statut : **spécification, aucun code métier**. Les éléments marqués
**À vérifier** n'ont pas été confirmés (source Microsoft ou test sur la plateforme).

## 1. Objet

Notifier les utilisateurs internes sur **Microsoft Teams** (application mobile et poste), avec pour chaque
notification un **titre**, un **texte** et une **adresse précise** à ouvrir, derrière un **bouton dont le libellé dépend
du type** de notification.

Pourquoi : l'e-mail n'est pas toujours adapté, et le push de la PWA d'Odoo dépend du système du téléphone, du navigateur
et d'un onglet ouvert. La plupart des utilisateurs ont déjà Teams. Odoo Enterprise résout ce besoin avec son application
mobile native (module `ocn_client`, relais FCM/APNs) ; Community n'a pas d'équivalent.

Premiers usages : `expense_core` et `expense_qonto` (dépense par carte à compléter, relances, alertes de seuil,
remboursement à rattacher, bon à payer). Le module est générique : il ne dépend que de `mail`.

## 2. Décisions prises

| Sujet | Décision |
|---|---|
| Canal | Teams est un **troisième `notification_type`** de `res.users`, à côté de `email` et `inbox` |
| Boîte Odoo | **Teams remplace** la boîte de réception d'Odoo pour l'utilisateur qui le choisit |
| Envoi | Pas d'appel HTTP dans la transaction : file d'attente, déclenchée à la demande par `_trigger()` |
| Rattrapage | Tâche planifiée **toutes les 10 minutes** (échecs, délais dépassés) |
| Transport | Remplaçable ; à trancher en phase 0 (§7) |
| Version d'Odoo | 18 : adresses de la forme `/odoo/<modèle>/<id>` ou `/odoo/action-<xmlid>/<id>` |

## 3. Limite d'usage à connaître

Toucher la bannière Teams ouvre Teams, **pas l'adresse**. L'utilisateur touche ensuite le bouton de la carte. C'est un
clic de plus que le push PWA actuel, mais l'arrivée est fiable. **À vérifier** en phase 0 : ce que la bannière affiche
(titre et texte, ou seulement « a envoyé une carte »), sur iOS et Android, application fermée.

## 4. Fonctionnement

1. Un module appelle `message_notify` / `message_post` (comme `_expense_notify` aujourd'hui), avec des clés de contexte
   facultatives : type de notification, adresse, libellé du bouton.
2. `_notify_get_recipients` marque `notif = 'teams'` pour les destinataires dont `notification_type = 'teams'` : ni
   boîte Odoo ni e-mail pour eux.
3. `_notify_thread_by_teams` (appelé à la suite des trois méthodes d'envoi d'Odoo : boîte, e-mail, web push) crée une
   ligne dans la file d'attente et une trace `mail.notification` de type `teams`, puis déclenche la tâche d'envoi :
   `cron._trigger()`, comme le fait `mail` pour son push. L'envoi part dans les secondes qui suivent la validation de la
   transaction.
4. La tâche d'envoi appelle le transport. Succès : état `sent`. Échec : nouvel essai par la tâche de rattrapage.
5. Après le nombre maximal d'essais, l'envoi est en échec définitif : **repli sur l'e-mail** (le destinataire ne doit pas
   rester sans notification, puisque Teams remplace la boîte Odoo), avec une trace dans le chatter.

Le web push de la PWA (`_notify_thread_by_web_push`) est indépendant du `notification_type` et reste actif pour les
utilisateurs qui ont installé la PWA. À décider : le laisser actif ou le couper pour les utilisateurs Teams.

## 5. Modèles

### 5.1 `res.users` (hérité)
- `notification_type` : `selection_add=[('teams', 'Gérer dans Microsoft Teams')]`, `ondelete={'teams': 'set default'}`.
- Groupe `notify_teams.group_notification_type_teams`, exclusif du groupe `mail.group_mail_notification_type_inbox`.
- Remplacer le **calcul** et l'**inverse** de `notification_type` : en 18 la valeur est déduite de l'appartenance au groupe
  inbox (`mail/models/res_users.py:36-58`) et le calcul d'Odoo ramènerait `teams` à `email`. L'inverse doit gérer les deux
  groupes. **À vérifier par test** : changement de groupe, création, conversion en utilisateur portail.
- La contrainte SQL d'Odoo (`email` ou non partagé) convient : Teams est réservé aux utilisateurs internes.
- `teams_upn` (facultatif) : identifiant Teams si différent de l'e-mail Odoo. Par défaut, l'e-mail.
- Aucun champ de configuration par utilisateur : le choix se fait dans les préférences (bouton radio, déjà affiché par
  `mail`). **À vérifier** : affichage du troisième choix.

### 5.2 `mail.notification` (hérité)
- `notification_type` : `selection_add=[('teams', 'Microsoft Teams')]`, `ondelete={'teams': 'cascade'}`. La trace
  porte l'état d'envoi, visible dans le chatter.

### 5.3 `notify.type` (registre)
| Champ | Rôle |
|---|---|
| `code` | clé technique, unique, ex. `expense_card_to_complete` |
| `name` | libellé affiché en configuration |
| `action_label` | libellé du bouton principal, ex. « Ajouter le justificatif » |
| `target` | cible par défaut de l'adresse : fiche, action Odoo, adresse libre |
| `secondary_actions` | 0 à 2 boutons secondaires (libellé, cible) |
| `active` | désactive Teams pour ce type |
| `fallback_to_inbox` | à décider (§9) |

Chaque module déclare ses types en XML. L'appelant peut surcharger l'adresse et le libellé à chaque envoi. Sans type,
le type par défaut s'applique : bouton « Ouvrir » vers la fiche.

### 5.4 `notify.teams.message` (file d'attente)
Destinataire (`res.partner`), type, titre, texte, adresse, libellé du bouton, boutons secondaires (JSON), message Odoo
d'origine, **clé anti-doublon** (destinataire + message + type), état (`queued`, `sent`, `failed`, `canceled`), nombre
d'essais, prochain essai, dernière erreur, dates de création et d'envoi.

### 5.5 Configuration (`res.config.settings`, paramètres système)
Transport actif, adresse d'appel du transport et **secret partagé** (visible seulement du groupe système), nombre
maximal d'essais (5), plafond d'envois par destinataire et par heure, durée de conservation (envoyés : 30 jours,
échecs : plus longtemps).

## 6. Envoi

### 6.1 Tâches planifiées
- **Envoi** : déclenchée par `_trigger()` à chaque ajout dans la file. Traite par lots, `FOR UPDATE SKIP LOCKED`.
- **Rattrapage et purge** : toutes les **10 minutes**. Reprend les `queued` dont le prochain essai est échu, avec
  délai croissant entre essais, bascule en `failed` après le maximum, supprime les lignes anciennes.
- **Délai côté Odoo** (lu dans `ir_cron.py:644-711` et `service/server.py:479-520`) : `_trigger()` crée une ligne de
  déclenchement « maintenant », puis, **après la validation de la transaction**, émet une notification PostgreSQL
  `cron_trigger`. Les processus de tâches planifiées y sont à l'écoute et se réveillent aussitôt (décalage de quelques
  centièmes de seconde) ; sans notification, ils ne se réveillent que toutes les 60 secondes environ. L'envoi démarre
  donc en une à deux secondes après la validation, **à ces conditions** :
  - au moins un processus de tâches planifiées tourne en production (en développement, `workers` et `max_cron_threads`
    sont à leurs valeurs par défaut) : **à vérifier** ;
  - PostgreSQL n'est pas en mode « récupération » (réplica en lecture seule) : Odoo écrit alors un avertissement et
    retombe sur l'attente de 60 secondes : **à vérifier** ;
  - la tâche d'envoi n'est pas déjà occupée par un lot : l'envoi suivant attend la fin du lot, d'où des lots courts.
- Ce délai ne couvre que la partie Odoo : le délai jusqu'à la bannière dépend de Microsoft (§7, mesure à faire).

### 6.2 Transport remplaçable
Interface `_send(message)` ; le choix se fait dans la configuration.

**Décision : Odoo construit la carte adaptative** (titre, texte, boutons, libellés issus du registre des types, §5.3) et
l'envoie toute prête. Le flux côté Microsoft reste générique : un nouveau type de notification ne demande aucune
modification chez Microsoft. Contenu envoyé, identique pour tous les transports :

```json
{
  "secret": "<secret partagé>",
  "target_email": "prenom.nom@exemple.com",
  "card": {
    "type": "AdaptiveCard",
    "version": "1.4",
    "body": [
      {"type": "TextBlock", "text": "Dépense par carte à compléter", "weight": "Bolder", "size": "Medium"},
      {"type": "TextBlock", "text": "Votre carte a payé 42,00 EUR chez ...", "wrap": true}
    ],
    "actions": [
      {"type": "Action.OpenUrl", "title": "Ajouter le justificatif",
       "url": "https://odoo.exemple.com/odoo/action-expense_core.action_expense_open_receipt/123"},
      {"type": "Action.OpenUrl", "title": "Voir la fiche", "url": "https://.../odoo/accounting/..."}
    ]
  }
}
```

Le flux vérifie le secret, puis publie `card` en message privé à `target_email`. Le texte est tronqué à une taille
raisonnable. **Limite** : la réponse du flux (202, « accepté ») ne dit pas que le message est arrivé dans Teams ; un échec
du flux (adresse inconnue, plafond atteint, flux désactivé) est invisible pour Odoo et ne déclenche pas le repli sur
l'e-mail (§4, point 5). À traiter en phase 0 (réponse du flux après publication si le déclencheur le permet, alertes
d'échec au propriétaire du flux, contrôle périodique).

Six options ont été étudiées, détaillées ci-dessous ; la synthèse est au §6.2.7. Ordre de préférence pour la phase 0 :
6.2.1, puis 6.2.2, puis 6.2.3 ; 6.2.4 en second rang ; 6.2.5 différée ; 6.2.6 écartée.

#### 6.2.1 Webhook « Workflows » de Teams (déclencheur « Teams webhook »)
Constats de la recherche (novembre 2026) :
- Microsoft indique que [ces modèles et le déclencheur Teams webhook ne demandent pas de licence premium](https://support.microsoft.com/en-us/teams/apps-service/create-incoming-webhooks-with-workflows-for-microsoft-teams).
  Il existe trois modèles pour une conversation et trois pour un canal (tous, personnes précises, personnes de
  l'organisation), en carte adaptative ou « Message Card ». Le modèle de conversation peut être indisponible selon le
  type d'organisation.
- **Le destinataire est choisi à la création du flux** (équipe et canal, ou conversation) : aucune source ne montre
  qu'un modèle le lit dans la requête. Il faut donc **partir d'un flux vide** : déclencheur « Teams webhook » +
  action « Publier une carte dans une conversation ou un canal » (Post in : conversation avec Flow bot, Recipient :
  `target_email` du JSON). Que cette modification garde le flux sans licence premium n'est **pas confirmé** : à tester.
- **Identité de l'expéditeur** : le flux est attaché au compte qui le crée, et son envoi dépend de ce compte (flux
  cassé si le compte est supprimé). Un fil Microsoft Q&A rapporte que l'option « Flow bot » a posté au nom de
  l'utilisateur dans un canal ([question](https://learn.microsoft.com/en-us/answers/questions/1823183/teams-webhook-request-workflow-is-posting-as-user)),
  sans solution officielle, et que le contournement est un **compte de service** avec une licence Teams, membre des
  équipes concernées. À vérifier pour une conversation privée avec le destinataire.
- **Authentification** : l'option « Anyone » repose sur la signature dans l'adresse (traitée comme un secret) ;
  Microsoft la présente comme un réglage historique sans authentification supplémentaire. Le secret partagé du JSON
  (contrôlé par une condition dans le flux) reste donc obligatoire.

- **Vérification : l'utilisateur n'a rien à installer** (recherche de novembre 2026). Le chemin « Publier en tant que Flow bot,
  dans une conversation avec Flow bot, destinataire = un utilisateur » est décrit par Microsoft sans aucune étape côté
  destinataire ([Power Automate : envoyer un message dans Teams](https://learn.microsoft.com/power-automate/teams/send-a-message-in-teams)).
  La seule condition citée est côté administrateur, une fois pour tous : l'application **Workflows** (anciennement
  Power Automate) doit être à l'état « autorisée » dans le centre d'administration Teams
  ([connecteur Teams](https://learn.microsoft.com/en-us/connectors/teams/)). Contre-exemple utile : pour les agents
  Copilot Studio, la même page exige que le destinataire ait le bot installé, ce qui n'est pas dit pour Flow bot.
  Aucune source ne décrit explicitement le premier message reçu par quelqu'un qui n'a jamais été contacté : à confirmer
  en phase 0 sur un compte de test.
- **Limites relevées** : une seule adresse destinataire par appel ([problème signalé](https://github.com/MicrosoftDocs/msteams-docs/issues/14497),
  à ne pas supposer corrigé) ; les actions d'écriture « Flow bot » sont plafonnées à **25 appels par 300 secondes par
  connexion** (connecteur Teams), soit une cinquantaine de messages au plus en 10 minutes par flux : prévoir un débit
  limité dans la file d'attente d'Odoo (§5.5) et la reprise sur erreur 429 ; l'option « Flow bot » n'existe pas dans
  les clouds gouvernementaux (sans objet ici).

##### 6.2.1.1 Principe
Odoo envoie un POST avec du JSON à l'adresse d'un petit programme (un « flux ») hébergé chez Microsoft. Le flux écrit à
l'utilisateur dans Teams, en message privé du bot « Workflows » (« Flow bot » dans l'éditeur), avec une carte et ses
boutons. Trois acteurs : **Odoo**, **le flux** (chez Microsoft, rattaché à un compte de service) et **Teams**.

##### 6.2.1.2 Ce qu'il faut coder (côté Odoo uniquement)
Rien n'est du code chez Microsoft : le flux se construit dans l'éditeur. Transport « webhook » de `notify_teams` :

```python
def _send_webhook(self, message):
    payload = {
        "secret": self._get_param("notify_teams.secret"),
        "target_email": message.partner_id.user_ids.teams_upn or message.partner_id.email,
        "card": message._build_adaptive_card(),   # carte complète, prête à afficher
    }
    resp = requests.post(self._get_param("notify_teams.webhook_url"), json=payload, timeout=10)
    resp.raise_for_status()   # 4xx/5xx : la ligne de la file repartira au rattrapage
```

`_build_adaptive_card()` fabrique le JSON de la carte à partir du titre, du texte, de l'adresse et du libellé du bouton
(registre des types, §5.3). Le reste est décrit ailleurs : troisième `notification_type` (§5.1), file d'attente (§5.4),
tâches planifiées (§6.1), configuration (§5.5).

##### 6.2.1.3 Paramétrage, pas à pas
Chez Microsoft (administrateurs Microsoft 365 et Teams) :
1. Choisir ou créer un **compte de service** (par exemple `odoo-notifications@votre-domaine`) avec une licence
   Microsoft 365 qui inclut Teams **et** « Power Automate pour Microsoft 365 » (Business Basic ou supérieur, à
   environ 6-7 $ par mois d'après des sources tierces). **Teams Essentials est à éviter** : le comparatif des offres de
   Microsoft ([PDF](https://cdn-dynmedia-1.microsoft.com/is/content/microsoftcorp/microsoft/final/en-us/microsoft-brand/documents/modern-work-plan-comparison-smb.pdf))
   n'y coche pas « Power Automate for Microsoft 365 » et y marque « N/A » les extensions Power Automate (lecture des
   colonnes déduite de la mise en page du PDF, document de la vente par partenaire, date non indiquée : à confirmer
   auprès de l'administrateur Microsoft 365).
2. Dans le centre d'administration Teams, vérifier que l'application **Workflows** est autorisée (seule condition
   côté administrateur relevée dans la documentation).
3. Se connecter à Teams avec le compte de service, ouvrir l'application Workflows et créer un flux **vide**.
4. Déclencheur : « Lorsqu'une requête de webhook Teams est reçue ». « Qui peut déclencher le flux » : « Tout le monde »
   (Odoo ne peut pas s'authentifier chez Microsoft).
5. Condition : si `secret` du JSON diffère du secret attendu, arrêter le flux (§6.3.1).
6. Action « Publier une carte dans une conversation ou un canal » : **Publier en tant que** Flow bot, **Publier dans**
   Conversation avec Flow bot, **Destinataire** = `target_email`, **Carte adaptative** = `card`.
7. Enregistrer et copier l'adresse du déclencheur. À ne pas diffuser ; le vrai garde-fou est le secret (§6.3.1).
8. Ajouter deux co-propriétaires au flux et prévoir sa sauvegarde et ses alertes d'échec (§6.3.2).

Dans Odoo :
1. Renseigner l'adresse du flux et le secret dans la configuration de `notify_teams`.
2. Bouton « Envoyer un test » : envoie une carte à l'utilisateur courant, pour valider l'ensemble.
3. Chaque utilisateur choisit « Gérer dans Microsoft Teams » dans ses préférences. L'adresse e-mail Odoo doit être celle
   de son compte Microsoft, ou son identifiant Teams renseigné à part (`teams_upn`, §5.1).

Non confirmés, à vérifier en phase 0 : que ce flux « vide » reste sans licence premium, et les libellés exacts de
l'éditeur.

##### 6.2.1.4 Déroulement d'une notification
Exemple : une transaction par carte arrive par le webhook Qonto, pour un porteur qui a choisi Teams.
1. Odoo crée la dépense, puis appelle `_expense_notify`, donc `message_notify`.
2. Le destinataire est en mode Teams : ni boîte Odoo ni e-mail. Odoo ajoute une ligne dans la file et lance la tâche
   d'envoi (`_trigger()`). Le webhook Qonto reçoit sa réponse immédiatement.
3. La tâche envoie le POST au flux avec l'adresse e-mail du porteur (une à deux secondes après la validation de la
   transaction, aux conditions du §6.1).
4. Microsoft répond tout de suite « reçu » (202) ; Odoo marque la ligne `sent`.
5. Le flux s'exécute chez Microsoft : vérifie le secret, retrouve l'utilisateur par son adresse dans l'annuaire, ouvre la
   conversation privée avec le bot, publie la carte. Délai non documenté (§7, mesure à faire).
6. Le téléphone affiche la bannière Teams (expéditeur « Workflows »).
7. L'utilisateur touche la bannière : **Teams s'ouvre sur la conversation**, pas sur Odoo. Il touche le bouton (par
   exemple « Ajouter le justificatif ») : le navigateur ouvre l'écran simplifié du reçu dans Odoo, avec la connexion
   Office 365 si besoin.

**Limite** (déjà signalée au §6.2) : à l'étape 4, « envoyé » veut dire « accepté par le flux », pas « arrivé dans
Teams ». Un échec du flux (adresse inconnue chez Microsoft, plafond de 25 appels par 5 minutes, flux désactivé, compte de
service bloqué) est invisible pour Odoo et ne déclenche pas le repli sur l'e-mail. Pistes pour la phase 0 : faire
répondre le flux à Odoo après la publication (non vérifié pour ce déclencheur), et recevoir par e-mail les échecs
d'exécution du flux.

#### 6.2.2 Logic App Azure (mode Consommation)
- Pas de licence par utilisateur, facturation à l'exécution (tarif exact non vérifié), restriction possible par adresse IP.
- Demande un abonnement Azure et un compte de service pour la connexion Teams (jetons à surveiller).
- Même carte et même action que Power Automate. **À vérifier** : l'option « publier en tant que Flow bot » y est
  identique à celle de Power Automate.

#### 6.2.3 Power Automate (déclencheur HTTP)
- Licence **Power Automate Premium** (environ 13 à 15 € par mois et par utilisateur, d'après des sources tierces) sur le
  **seul compte propriétaire du flux**. À faire confirmer par l'administrateur Microsoft 365.
- Même flux et même carte que 6.2.1, avec le déclencheur « Lorsqu'une requête HTTP est reçue ».

#### 6.2.4 Graph direct (message de conversation envoyé par Odoo, sans flux)
- L'envoi d'un message de conversation n'accepte les droits d'application que pour la migration de données
  ([documentation](https://learn.microsoft.com/en-us/graph/api/chatmessage-post)). Il faut donc un jeton **délégué** d'un
  compte de service (`Chat.Create`, `ChatMessage.Send`, `offline_access`), renouvelé par jeton d'actualisation.
- Le message part au nom de ce compte, pas d'un bot. Demande une inscription d'application Entra, un écran de connexion
  du compte dans le module, et la gestion de l'expiration (accès conditionnel, authentification multifacteur).
- La connexion OAuth Microsoft de `taz-common` ne peut pas servir : `auth_oauth` d'Odoo 18 n'implémente que le flux
  implicite (`response_type='token'`, `auth_oauth/controllers/main.py:64`), qui ne renvoie jamais de jeton
  d'actualisation chez Microsoft, avec ou sans `offline_access` (essai déjà fait). Le module devrait implémenter lui-même
  le **flux par code d'autorisation** (une connexion ponctuelle du compte de service, jeton d'actualisation stocké dans un
  paramètre protégé) ; le login des utilisateurs n'est pas concerné (voir l'**Annexe A**, même mécanisme pour les
  utilisateurs).
- Les cartes adaptatives dans un message Graph sont à tester ; la documentation de l'API ne cite que l'action `OpenUrl`
  pour les cartes, ce qui suffit au besoin.

#### 6.2.5 Bot Teams (message proactif)
- Application Azure Bot, adresse publique appelée par Microsoft, **application Teams installée chez chaque utilisateur**
  (poussée par l'administrateur Teams ou installée par Graph).
- Le message vient d'un vrai bot, avec une carte et des boutons ; aucun compte de service. Tarif de l'inscription Azure
  Bot et hébergement non vérifiés.
- Différée : lourdeur de mise en place pour la première version.

#### 6.2.6 Notification du fil d'activité (`sendActivityNotification`) : exclue
Notification qui arrive dans le fil « Activité » de Teams, sans conversation, avec des droits d'**application**
(`TeamsActivity.Send.User` ou `TeamsActivity.Send`) : ni compte de service, ni jeton d'utilisateur, ni licence Power
Automate. Sources : [guide](https://learn.microsoft.com/en-us/graph/teams-send-activityfeednotifications),
[API](https://learn.microsoft.com/en-us/graph/api/userteamwork-sendactivitynotification).

**Décision : exclue**, pour trois raisons, chacune suffisante :

1. **Pas de bouton.** La notification entière est le seul élément cliquable, avec un seul lien. Le libellé d'action
   propre à chaque type (§5.3) n'existe pas ; il ne pourrait figurer que dans le texte d'aperçu (150 caractères).
2. **Le lien ne peut pas ouvrir Odoo directement.** L'adresse (`webUrl`) d'un sujet de type texte doit commencer par le
   domaine Teams ; la documentation ne mentionne aucun site externe. Le clic ouvrirait l'application Teams, sur un
   onglet qui affiche Odoo dans Teams, et non Odoo dans le navigateur du téléphone, avec son écran simplifié du reçu et
   son authentification.
3. **Une application Teams à construire et à déployer** (manifeste version 1.7 ou plus, `webApplicationInfo`), installée
   chez chaque destinataire (poussée par l'administrateur Teams ou installée par Graph), avec des types d'activité
   déclarés dans le manifeste pour des libellés différents (une nouvelle notification demande une nouvelle version de
   l'application).

À cela s'ajoute un risque sur le mobile : le guide indique que iOS et Android n'affichent ces notifications que si
l'application est sur une liste d'autorisation, et que seules les applications tierces sont gérées. Cela va contre
l'objectif du module (une alerte mobile fiable). À reconsidérer seulement si Microsoft autorise un lien externe et
supprime cette contrainte mobile.

#### 6.2.7 Tableau de synthèse

| Option | Coût (licence) | Compte délégué (de service) | Jeton d'actualisation, donc flux par code à coder dans Odoo | Manipulation par chaque utilisateur (installer une application Teams) | Bouton qui ouvre la bonne page d'Odoo |
|---|---|---|---|---|---|
| 6.2.1 Webhook « Workflows » | Aucune licence premium d'après Microsoft ; à confirmer avec un destinataire dynamique | Oui : le flux appartient à un compte, de service recommandé | Non : Odoo envoie un simple appel HTTP, la connexion Teams est gérée par le flux | Non d'après la documentation Microsoft (vérifié, voir 6.2.1) ; reste à confirmer sur un utilisateur qui n'a jamais reçu de message | Oui (`Action.OpenUrl`) |
| 6.2.2 Logic App Azure | Pas de licence par utilisateur ; abonnement Azure, facturation à l'exécution | Oui : connexion Teams du connecteur | Non côté Odoo ; jeton de la connexion à surveiller côté Azure | Non | Oui |
| 6.2.3 Power Automate (HTTP) | Power Automate Premium, environ 13 à 15 € par mois, sur le seul compte propriétaire | Oui : propriétaire du flux, de service recommandé | Non côté Odoo | Non | Oui |
| 6.2.4 Graph direct | Pas de licence dédiée ; le compte de service a besoin d'une licence Teams | Oui : jeton délégué | **Oui** : flux par code d'autorisation, `offline_access`, renouvellement (Annexe A) | Non | Oui (carte avec `OpenUrl`, à tester) |
| 6.2.5 Bot Teams | Inscription Azure Bot et hébergement, tarif non vérifié | Non : identité du bot | Non : jeton d'application (secret ou certificat) | **Oui** : application installée chez chacun (ou poussée par l'administrateur Teams) | Oui (carte avec `OpenUrl`) |
| 6.2.6 Fil d'activité (exclue) | Aucune licence Power Automate ; inscription Entra gratuite | Non : droits d'application | Non | **Oui** : application Teams installée chez chacun | **Non** : un seul lien, vers un domaine Teams, sans libellé |

Lecture : seules les options 6.2.1 à 6.2.3 cumulent « aucune action côté utilisateur » et « bouton vers Odoo » sans
code OAuth dans Odoo ; elles ne diffèrent que par la licence et par l'abonnement Azure. La 6.2.4 supprime le flux
Microsoft mais demande le code OAuth. La 6.2.5 et la 6.2.6 demandent une application Teams déployée chez chacun.

### 6.3 Sécurité
- L'adresse d'appel et le secret ne sont lisibles que par le groupe système.
- Aucune donnée plus sensible que le texte de la notification ne part vers Teams. À valider : montants et libellés de
  dépenses autorisés ou non dans Teams.
- Les écritures de la file passent par `sudo` ; les utilisateurs n'y ont aucun accès direct.

#### 6.3.1 Adresse du flux et secret partagé (transports par flux, §6.2.1 à 6.2.3)
- Le déclencheur du flux est réglé sur « Tout le monde » (Odoo ne peut pas s'authentifier chez Microsoft). La seule
  protection native est la signature contenue dans l'adresse ; le **secret partagé du JSON**, vérifié par une condition
  en tête de flux, est le vrai garde-fou. Sans cette condition, l'adresse seule permettrait d'écrire à n'importe qui.
- **Avec la condition, l'adresse seule est quasi inoffensive**, mais pas anodine : chaque appel démarre une exécution du
  flux. Qui connaît l'adresse sans le secret ne peut rien envoyer, mais peut consommer les quotas et atteindre le plafond
  de débit du connecteur (§6.2.1), retardant ou faisant échouer les vraies notifications : risque de déni de service léger.
- **Le secret est aussi confidentiel que l'adresse** : il circule dans le corps de chaque requête, et il est stocké au même
  endroit (configuration d'Odoo). Une fuite de la base ou d'une sauvegarde expose les deux ; la double protection ne vaut
  que contre une fuite de l'adresse seule (journaux d'un intermédiaire, capture d'écran, copie dans un courriel).
- **La protection dépend de la bonne construction de la condition** : tests de phase 0 obligatoires (appel sans secret,
  appel avec un mauvais secret : rien ne doit être envoyé).
- **Rotation** : prévoir la procédure de changement de l'adresse (régénération du déclencheur) et du secret, dans le flux
  et dans Odoo en même temps, pour éviter une coupure.

#### 6.3.2 Exploitation du flux et du compte de service
- Le flux appartient au compte qui l'a créé et tourne avec la connexion Teams de ce compte : s'il est désactivé, le flux
  s'arrête et Odoo continue de recevoir « accepté » (§6.2). Créer le flux avec le **compte de service**, pas avec un
  compte personnel.
- Ajouter **deux administrateurs comme co-propriétaires** du flux (ils peuvent le modifier, le réactiver et lire son
  historique). Le co-propriétaire n'évite pas tout : si le compte d'origine est désactivé, il doit **remplacer la connexion
  Teams** du flux par la sienne.
- Surveiller le compte de service lui-même : expiration du mot de passe, authentification multifacteur, licence Teams.
  Chacune peut casser le flux de la même façon.
- Recevoir par e-mail les échecs d'exécution du flux (alerte au propriétaire), puisque Odoo ne les voit pas.
- Conserver une **exportation** du flux (sauvegarde de sa définition) pour le recréer au besoin.
- **Compte nominatif (celui d'une personne) : acceptable en phase 0 et pour un pilote seulement.** Un compte Microsoft
  personnel (outlook.com) ne convient pas : il faut un compte professionnel de l'annuaire. Avec « Flow bot », le
  destinataire voit l'expéditeur « Workflows » et non le nom du propriétaire, mais un fil Microsoft Q&A rapporte un cas
  où le nom de l'utilisateur apparaissait dans un canal : **à vérifier en phase 0** sur la conversation privée, en
  regardant ce qu'affiche le téléphone d'un autre utilisateur. Le nom du propriétaire reste visible dans la liste et
  l'historique des flux, côté administrateurs. Raisons de ne pas le garder en production : le flux s'arrête si la
  personne part ou si son compte est désactivé (et Odoo ne le voit pas, §6.2.1.4) ; un changement de mot de passe ou
  d'authentification multifacteur peut casser la connexion Teams ; les quotas (6 000 actions par 24 heures avec une
  licence Microsoft 365) sont ceux du propriétaire et partagés avec ses autres flux. Le gain, une licence mensuelle de
  quelques dollars, est faible face au risque. Le transfert de propriété d'un flux existant n'est pas toujours possible
  (il passe par une solution Power Automate : non vérifié) : créer le flux directement avec le bon compte.

## 7. Phase 0 : preuve de concept (avant tout code)

Un flux de test (webhook Teams, Logic App ou Power Automate), un `curl`, deux téléphones (iOS, Android), pour mesurer :
- ce que la bannière affiche, application fermée ;
- le délai de bout en bout (Qonto, webhook, `_trigger()`, flux, Teams) ;
- le destinataire dynamique par e-mail, et le comportement si l'e-mail ne correspond pas à un compte Teams ;
- le comportement du clic sur le bouton (ouverture dans le navigateur, authentification sur Odoo, joignabilité de
  l'adresse d'Odoo depuis le téléphone : HTTPS public ou VPN) ;
- le bouton principal plus un secondaire ;
- **le délai entre l'appel d'Odoo et l'arrivée de la notification**. Aucune source officielle ne donne de chiffre
  (recherche de novembre 2026). Les seuls repères sont des retours d'utilisateurs : une exécution de flux déclenchée par
  HTTP en 1 à 3 secondes, des délais de 6 à 20 secondes sur la réponse à une carte (autre scénario), des
  ralentissements attribués à la limitation de débit, et des échecs plutôt que des retards. Protocole : horodater l'envoi
  dans Odoo, relever dans l'historique des exécutions du flux l'heure du déclencheur et celle de l'action « Publier une
  carte », puis l'heure d'affichage de la bannière sur chaque téléphone ; répéter sur une vingtaine d'envois, avec et
  sans rafale, pour obtenir un délai médian et un pire cas ;
- avec un flux vide « Teams webhook » : le message arrive-t-il comme message privé du bot, ou au nom du compte de
  service ? Le flux reste-t-il sans licence premium une fois le destinataire dynamique ?

La phase 0 tranche le transport (§6.2) ; le module n'en dépend pas.

## 8. Intégration avec les modules de dépenses

- `expense_core` et `expense_qonto` dépendent de `notify_teams`.
- `_expense_notify` passe le type, l'adresse (`_expense_deep_link`) et le libellé du bouton. Le contexte
  `expense_push_url` et le service worker de `expense_core` restent pour la PWA.
- `expense_core/models/res_users.py` force `inbox` à la création des utilisateurs internes ; à revoir : le choix par
  défaut devient Teams, ou reste `inbox` tant que l'utilisateur n'a pas choisi.

Types de notification candidats (à valider) :

| Type | Bouton | Destination |
|---|---|---|
| Dépense par carte à compléter, et relances | Ajouter le justificatif | écran simplifié du reçu |
| Dépense au-dessus du seuil | Examiner la dépense | fiche de la pièce |
| Cumul de dépenses | Voir la dépense | dernière pièce du cumul |
| Remboursement par carte à rattacher (Office) | Rattacher l'avoir | fiche de l'avoir |
| Demande de bon à payer | Ouvrir la demande | fiche de la pièce |
| Nouveau message de messagerie | Ouvrir la conversation | message ou fiche |

## 9. Questions ouvertes

1. Transport retenu après la phase 0, et licence Power Automate Premium disponible ou non.
2. Les discussions Discuss (messages directs, canaux) passent-elles par Teams ? `discuss.channel` a son propre routage ;
   proposé : première version limitée aux notifications de fiches et à `message_notify`.
3. Web push de la PWA : actif ou coupé pour les utilisateurs Teams.
4. Repli sur l'e-mail après échec définitif : confirmé ici, mais pour tous les types ou seulement certains ?
5. Un bouton ou un principal plus un secondaire.
6. Montants et libellés de dépenses dans Teams : autorisé ou texte neutre.
7. Valeur par défaut du `notification_type` d'un nouvel utilisateur interne.
8. Joignabilité de l'adresse d'Odoo depuis les téléphones.

## 10. Plan

| Étape | Contenu |
|---|---|
| 0 | Preuve de concept (§7), décision du transport |
| 1 | `res.users` / `mail.notification` : troisième valeur, groupes, calcul et inverse ; tests de non-régression |
| 2 | Registre des types, file d'attente, tâches planifiées, transport, repli e-mail |
| 3 | Configuration et vues (préférences, types, file avec relance manuelle) |
| 4 | Branchement de `expense_core` et `expense_qonto` |

## 11. Tests prévus

- Changement de `notification_type` par l'utilisateur et par groupe, sans retour intempestif à `email`.
- Un utilisateur `teams` : pas de ligne de boîte Odoo ni d'e-mail, une ligne dans la file, une trace `teams`.
- Anti-doublon : un même message ne crée qu'un envoi par destinataire.
- Échec du transport : reprise à 10 minutes, échec définitif après le maximum, repli e-mail.
- `_trigger()` : l'envoi n'a pas lieu si la transaction est annulée.
- Libellé et adresse : type, surcharge par l'appelant, type par défaut.
- Webhook Qonto : la réponse n'attend pas Teams.

## Annexe A. Jeton d'actualisation Microsoft pour les utilisateurs (chantier `taz-common`)

Hors périmètre de `notify_teams`. Cette annexe détaille le chantier de `taz-common` signalé au §6.2.4 (Graph direct), car
le même mécanisme servirait aux deux.

### A.1 Situation actuelle
- La connexion « Office 365 » d'Odoo (`taz-common/models/oauth_microsoft_taz.py`) enregistre à chaque connexion le jeton
  d'accès de l'utilisateur et sa date d'expiration (`oauth_access_token`, `oauth_token_expires_at`).
- Ce jeton sert aux appels Microsoft Graph faits au nom de l'utilisateur : tâches Planner (`models/business_action.py`),
  messagerie (`/me/messages`, `models/res_users.py:140`), import des contacts (`models/office365_import_people.py`).
- `_get_valid_access_token` (`models/res_users.py:50-70`) ne renouvelle rien : jeton expiré, elle affiche une erreur
  qui demande à l'utilisateur de se déconnecter puis de se reconnecter. Le code de renouvellement existe, mais est
  commenté (« TODO »), de même que l'enregistrement du `refresh_token` (`oauth_microsoft_taz.py:95`).
- Le champ `oauth_refresh_token` existe, toujours vide.

### A.2 Cause
`auth_oauth` d'Odoo 18 Community n'implémente que le **flux implicite** (`response_type='token'`,
`addons/auth_oauth/controllers/main.py:64`). Chez Microsoft, ce flux ne renvoie jamais de jeton d'actualisation ;
`offline_access` n'a d'effet qu'avec le **flux par code d'autorisation**. Ajouter le scope dans le fournisseur ne sert à
rien (essai déjà fait, confirmé par le code).

**Pourquoi Odoo n'a pas implémenté le flux par code** (recherche de novembre 2026). Terminologie : le flux visé
s'appelle « flux par code d'autorisation » (*authorization code*) et non « flux explicite ». Sources consultées :
[#63965](https://github.com/odoo/odoo/issues/63965), [#63750](https://github.com/odoo/odoo/issues/63750),
[#289560](https://github.com/odoo/odoo/pull/289560).
- **Aucune explication officielle** n'a été trouvée. Le ticket [odoo/odoo#63965](https://github.com/odoo/odoo/issues/63965)
  (ouvert en janvier 2021, version 14.0) demande exactement ce changement : le flux implicite convient à une application
  qui s'exécute dans le navigateur, alors qu'Odoo appelle les fournisseurs depuis le serveur. Il est resté **ouvert, sans
  réponse d'Odoo** : un contributeur y renvoie vers le module OCA `auth_oidc`. Le ticket
  [#63750](https://github.com/odoo/odoo/issues/63750) signale en outre que le flux implicite d'Odoo n'envoie pas de
  `nonce`, ce qui gêne des fournisseurs comme Okta.
- **Hypothèses non confirmées** (aucune source ne le dit) : le module date d'une époque où le flux implicite était la
  pratique courante pour les connexions Google et Facebook ; il n'exige aucun secret côté serveur ni appel au point
  d'accès `token`, donc une configuration plus simple ; et il est resté tel quel par compatibilité.
- **Un correctif est en cours, mais pas dans la version 18** : la demande de fusion
  [odoo/odoo#289560](https://github.com/odoo/odoo/pull/289560) ajoute le flux par code avec PKCE à `auth_oauth`, en
  restant compatible avec les fournisseurs existants (le flux implicite reste le choix par défaut). Elle cible la branche
  `master` (pas la 18.0), a été ouverte en septembre 2026 et approuvée le 9 novembre 2026 ; elle n'était pas fusionnée au
  moment de la lecture. Elle ne mentionne pas le jeton d'actualisation : le flux par code n'y suffit pas à lui seul
  (voir A.8). L'ajout d'une fonctionnalité dans une version stable étant rare, ne pas compter sur un portage vers la 18.0
  (supposition).
- **Conclusion pour ce projet** : la conclusion de l'annexe ne change pas. Le chemin le plus sûr reste la **connexion
  séparée** « Connecter mon compte Office 365 » en flux par code, avec `offline_access` (voir A.7), plutôt que d'attendre
  une évolution d'`auth_oauth`. Le module OCA `auth_oidc` est l'alternative étudiée en A.8.

### A.3 Cible
Flux par code d'autorisation (avec PKCE, ou avec secret client puisque l'échange se fait côté serveur) :
1. Odoo redirige l'utilisateur vers `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/authorize` avec
   `response_type=code`, `state`, et les scopes : `openid profile email offline_access` + les scopes Graph déjà utilisés
   (Planner, messagerie, contacts : à relever dans le code avant de les déclarer).
2. Microsoft rappelle Odoo avec un code ; Odoo l'échange sur `.../oauth2/v2.0/token` (`grant_type=authorization_code`)
   contre un jeton d'accès, un jeton d'identité et un **jeton d'actualisation**.
3. Au renouvellement : `grant_type=refresh_token`. Microsoft renvoie un **nouveau** jeton d'actualisation à chaque
   échange : il faut l'enregistrer à chaque fois (l'ancien reste valable un temps, mais ne doit pas être conservé).
4. `_get_valid_access_token` renouvelle de façon transparente et ne demande de se reconnecter qu'en cas d'échec définitif
   (jeton révoqué, mot de passe changé, accès conditionnel).

Côté Microsoft Entra : l'inscription d'application doit déclarer l'adresse de retour (type « Web ») du nouveau
contrôleur, les autorisations **déléguées** correspondantes, et le consentement de l'administrateur si le tenant l'exige.

### A.4 Options d'implémentation
| Option | Contenu | Avis |
|---|---|---|
| 1. Module OCA `auth_oidc` (18.0) | Flux par code avec PKCE, validation du jeton d'identité par JWKS | Ne règle pas le problème à lui seul : il **ignore le jeton d'actualisation** et change le login de tous (voir A.8) |
| 2. Remplacer le flux dans `taz-common` | Nouveau contrôleur de connexion en flux par code, à la place du flux implicite | Touche à l'identification de tous les utilisateurs : risque le plus élevé |
| 3. **Connexion séparée « Connecter mon compte Office 365 »** | La connexion reste telle quelle ; un bouton (profil de l'utilisateur) lance le flux par code avec `offline_access` et n'enregistre que les jetons | **Recommandée** : n'affecte pas l'identification, livrable par étapes, et réutilisable par `notify_teams` pour son compte de service |

### A.5 Points d'attention
- **Un seul renouvellement à la fois par utilisateur** : deux requêtes simultanées qui renouvellent chacune avec le même
  jeton peuvent invalider l'un des deux. Prévoir un verrou (`SELECT ... FOR UPDATE` sur l'utilisateur).
- **Durée de vie** : le jeton d'actualisation expire s'il n'est pas utilisé pendant une longue période (de l'ordre de
  90 jours d'inactivité d'après ma connaissance de Microsoft, **à vérifier**) et peut être révoqué (changement de mot de
  passe, accès conditionnel). Garder le message actuel de reconnexion comme repli.
- **Sécurité** : un jeton d'actualisation est un secret durable. Le champ est déjà dans `USER_PRIVATE_FIELDS` ; limiter sa
  lecture au groupe système, ne jamais l'écrire dans les journaux, envisager de le chiffrer.
- **Migration** : les utilisateurs actuels n'ont pas de jeton d'actualisation ; chacun doit se connecter une fois par la
  nouvelle connexion pour en obtenir un.
- **Tests** : renouvellement réussi, jeton d'actualisation révoqué, deux renouvellements concurrents, utilisateur sans
  jeton (repli sur le message actuel).

### A.6 Lien avec `notify_teams`
Le transport « Graph direct » (§6.2.4) a besoin du même flux, pour un compte de service. Pour que `notify_teams`
reste générique (dépendance à `mail` seulement), placer ce flux dans un petit module commun, par exemple
`microsoft_oauth_code_flow`, dont dépendraient `taz-common` et, si ce transport est retenu, `notify_teams`.

### A.7 Détail de l'option 3 : connexion séparée « Connecter mon compte Office 365 »
Deux opérations distinctes, qui ne se mélangent pas :
- **S'identifier** (inchangé) : le bouton « Connexion Office365 » du login, en flux implicite, comme aujourd'hui.
- **Autoriser Odoo à agir pour soi** (nouveau) : un bouton dans les préférences de l'utilisateur, à cliquer une fois.

Déroulement :
1. L'utilisateur clique sur « Connecter mon compte Office 365 ». Un contrôleur construit l'adresse d'autorisation
   Microsoft (`response_type=code`, scopes `offline_access` + scopes Graph, `state` signé qui porte l'utilisateur et un
   jeton anti-falsification, PKCE) et le redirige.
2. Microsoft affiche l'écran de consentement (sans mot de passe si la session Microsoft est ouverte) et rappelle Odoo
   sur une adresse dédiée, par exemple `/microsoft_connect/callback`, avec un code.
3. Le contrôleur vérifie le `state`, échange le code contre les jetons sur le point d'accès `token` avec le secret du
   client, et enregistre jeton d'accès, jeton d'actualisation, expiration et scopes dans un modèle dédié
   (par exemple `microsoft.account`, rattaché à l'utilisateur).
4. Dès lors, `_get_valid_access_token` lit ce modèle et renouvelle en silence ; la connexion du login n'y touche plus.

**Compte de service de `notify_teams`** : même code. Un administrateur d'Odoo clique sur « Connecter le compte de
service » et s'authentifie chez Microsoft avec le compte de service (fenêtre de navigation privée) ; les jetons sont
rattachés à un enregistrement « compte de service » et non à un utilisateur. Un seul mécanisme, deux usages.

Côté Microsoft Entra :
- une adresse de retour de type **Web** pour le nouveau contrôleur, et un **secret client** ;
- les autorisations **déléguées** nécessaires, avec le consentement de l'administrateur si le tenant l'exige ;
- point d'attention : d'après ma connaissance de Microsoft (**à vérifier**), les jetons d'actualisation obtenus par une
  adresse de retour de type « application monopage » expirent au bout de 24 heures ; l'inscription actuelle, prévue pour
  le flux implicite, est probablement de ce type. Pour une durée longue, ajouter une adresse de type Web (même
  inscription ou nouvelle inscription).

Pourquoi ce choix plutôt que l'option 2 : le login n'est pas touché, donc aucun risque de verrouiller les utilisateurs ;
la livraison est progressive (utilisateur par utilisateur) ; la charge est limitée à un contrôleur, un modèle et le
renouvellement (charge non chiffrée à ce stade).

### A.8 Cas du module OCA `auth_oidc` (18.0)
Constats, lus dans le code de la branche 18.0 de [OCA/server-auth](https://github.com/OCA/server-auth/tree/18.0/auth_oidc) :
- Le module ajoute au fournisseur OAuth un champ **`flow`** : `access_token` (OAuth2), `id_token_code` (OpenID Connect,
  flux par code) et `id_token` (flux implicite, déconseillé), plus `client_secret`, `token_endpoint`, `jwks_uri`,
  `token_map` et `code_verifier` (PKCE).
- Le flux par code échange le code sur le point d'accès `token` (secret client en authentification Basic, adresse de
  retour `<racine>/auth_oauth/signin`). Il **ne lit que `access_token` et `id_token`** dans la réponse. Le jeton
  d'actualisation, même renvoyé par Microsoft avec `offline_access`, est **ignoré**. Le jeton d'accès n'est pas écrit
  sur l'utilisateur par ce module ; c'est `_auth_oauth_signin` qui le fait, c'est-à-dire, chez vous, votre surcharge.
- Le défaut `code_verifier` est une valeur aléatoire **du fournisseur**, donc partagée par tous les utilisateurs : le PKCE
  y protège moins qu'avec une valeur par connexion (constat de lecture, à confirmer sur le code complet).

Conséquences pour vous :
1. **Adopter `auth_oidc` ne suffit pas** à obtenir le jeton d'actualisation : il faudrait en plus surcharger la méthode
   d'échange du code pour conserver `refresh_token`, ce qui revient à recopier la méthode (aucun point d'extension).
2. **Cela remplace le login de tous les utilisateurs** (passage du flux implicite au flux par code) et demande d'adapter
   `taz-common` : votre `_auth_oauth_signin` lit `params['expires_in']` (`oauth_microsoft_taz.py`), que ce flux ne
   semble pas transmettre (à vérifier), et votre `_auth_oauth_validate` renvoie une validation vide au lieu d'analyser
   le jeton d'identité. La recherche d'utilisateur par `validation['mail']` suppose un `token_map` qui le fournisse.
3. Le module apporte un gain réel hors jeton d'actualisation : le login en flux par code est plus sûr que le flux
   implicite, que Microsoft déconseille aujourd'hui.

Recommandation : **ne pas lier les deux sujets**. Livrer la connexion séparée (A.7), qui règle le jeton d'actualisation
sans toucher au login ; envisager `auth_oidc` plus tard, comme chantier de sécurité du login, séparé.
