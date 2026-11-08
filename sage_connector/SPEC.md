# Spec — Export des écritures comptables vers Sage FRP 1000

## 1. Besoin

Exporter vers Sage FRP 1000, pour chaque **journal coché comme concerné**, les écritures comptables
des `account.move` validés — sous forme d'écritures (lignes débit/crédit par compte général), pas
sous forme de "facture" au sens Sage. Le déclenchement est synchrone à la validation (comme la
plupart des connecteurs d'export comptable de ce type côté OCA : un flag d'activation par journal,
un statut de synchronisation sur la pièce, un envoi groupé des pièces en attente).

Complément tiers : pouvoir retrouver/créer automatiquement le tiers côté Sage à partir des
informations déjà connues côté Odoo (SIRET, TVA intracom, nom), pour renseigner les codes tiers
Sage utilisés comme compte auxiliaire dans les écritures.

## 1bis. Engagement réciproque

**Précondition organisationnelle à toute cette spec, pas juste une règle technique** : une écriture
importée dans Sage via ce connecteur **ne doit jamais être modifiée ni supprimée directement dans
Sage, par personne** — ni Denis, ni le Groupe AF, ni MC2I, ni quiconque ayant un accès direct à
Sage (interface utilisateur ou autre outil). Odoo est la source de vérité pour ces écritures ; toute
correction nécessaire doit repasser par Odoo (facture d'avoir, contre-passation, etc.), jamais par
une modification manuelle côté Sage.

**Pourquoi c'est indispensable, pas juste une bonne pratique** : le connecteur ne fait **aucune
vérification de réconciliation** après l'envoi — une fois `sage_sync_status = sent` (§5.1), Odoo ne
relit jamais l'écriture côté Sage pour vérifier qu'elle y est toujours identique. Si quelqu'un
modifie ou supprime manuellement une écriture envoyée :
- Odoo continuera d'afficher `sent` sans jamais détecter la divergence (pas de mécanisme pour s'en
  apercevoir, ni dans cette spec ni prévu ailleurs) ;
- une correction faite uniquement côté Sage ne sera **jamais répercutée dans Odoo**, qui restera la
  source de vérité officielle mais incohérente avec la réalité comptable chez Sage ;
- ça invaliderait aussi la logique de blocage de suppression (§6.2) et le constat du §3 (aucune
  modification/suppression possible via l'API) : ces règles supposent que la seule porte d'entrée
  vers ces écritures est le connecteur, pas une main humaine côté Sage.

**Ce que ça implique concrètement** :
- ce point doit être communiqué explicitement aux personnes ayant un accès direct à Sage (Denis,
  Groupe AF, MC2I) avant la mise en prod — ce n'est pas quelque chose que le code peut imposer tout
  seul, c'est un engagement entre équipes ;
- toute anomalie détectée côté Sage (solde qui ne correspond plus à Odoo, écriture introuvable) doit
  être traitée comme un incident à remonter, pas corrigée localement dans Sage ;
- si un jour une détection automatique de divergence s'avère nécessaire (comparer périodiquement les
  écritures envoyées avec leur état réel côté Sage), ce serait une évolution à ajouter — **hors
  périmètre de cette spec** (cf. §9), qui part du principe que l'engagement ci-dessus est respecté.

## 2. Ce qui est déjà confirmé (acquis, ne pas re-découvrir)

- Authentification : token Bearer statique, dans le header `Authorization`.
- URL : `{base_url}/compta/server/sdata/{contrat}/{entité}` où `{contrat}` = `MC2I` **seul** (pas
  `MC2I/GenEnvironnement` — erreur initiale corrigée). Chaque entité porte son propre "dataset"
  dans son chemin (`gentiers/TTiers`, `cptimport/tsessionimport`, etc.).
- Entité tiers : `gentiers/TTiers` (maître), `gentiers/TClient` et `gentiers/TFournisseur` (rôles).
- **Recherche directement sur un rôle par jointure vers `TTiers`, confirmée fonctionnelle** (correction
  d'une conclusion erronée précédente qui pensait la jointure limitée à `tiers.code`) :
  `TClient(tiers.codeSIRET eq '...')` et `TFournisseur(tiers.codeSIRET eq '...')` renvoient 200 avec le
  tiers complet, `Caption` portant directement le code du rôle (ex: `"411TASMANE"`). Idem avec
  `tiers.numeroTVACEE eq '...'` (TVA intracom, cf. ci-dessous) et `tiers.code eq '...'`. **Pas besoin
  de chercher `TTiers` séparément puis de résoudre le rôle** : interroger directement `TClient` et
  `TFournisseur` avec le critère disponible (SIRET, TVA, etc.) suffit à obtenir le code du rôle.
- Champ TVA intracommunautaire sur `TTiers` : **`numeroTVACEE`** (confirmé).
- **Autre bizarrerie de données observée sur ce dossier MC2I** : sur un même tiers "métier" ayant
  ses deux `TTiers` (client + fournisseur, cf. remarque en tête de §8), le **SIRET n'est renseigné
  que sur l'un des deux** — plutôt le `TTiers` côté fournisseur dans les cas observés. Donc une
  recherche par SIRET sur le rôle qui n'a pas le SIRET renseigné renvoie 0 résultat, **pas parce que
  ce rôle n'existe pas**, mais parce que la donnée est absente sur cet enregistrement précis. Même
  type de risque de données incomplètes à garder en tête pour la TVA intracom et les autres critères
  de la cascade (§8.1) — rien ne garantit qu'un critère soit renseigné de façon homogène sur les deux
  `TTiers` d'un même tiers.
- Import d'écritures : 3 étapes dans un seul `$batch` —
  1. `POST cptimport/tsessionimport` (session, avec `Caption` et éventuellement `CompteEquilibre`)
  2. `POST cptimport/timportecritures` × N (une par ligne), chacune liée à la session via
     `"SessionImport": {"$uuid": "<uuid session>"}`
  3. `EXEC cptimportmouvement/TProcessusPlanifImport/Executer`, avec `$resource.codeSessionInf` et
     `$resource.codeSessionSup` = **le `Caption` de la session, en toutes lettres, pas une référence
     `$uuid`** — et sa propre directive `"$": {"$transaction": ["commit"]}`.
  4. Enveloppe racine du `$batch` : `{"$": {"$transaction": ["atomic"]}, "$resources": [...]}`.
- Un `$httpStatus` 200 sur l'exécution **ne garantit pas** que l'écriture a été acceptée : les
  erreurs métier (ex: pièce déséquilibrée) apparaissent comme texte dans `$messages`, au milieu de
  messages de succès pour d'autres pièces du même lot.
- Création d'un tiers : `$batch` à 2 opérations — `POST gentiers/TTiers` (avec `$uuid`) puis
  `EXEC gentiers/TTiers(<$uuid>)/creerFournisseur` (ou vraisemblablement `creerClient`, symétrique,
  non testé). La réponse à un `POST` en `$batch` ne contient **pas** de `$key` — seulement un
  `$location` du type `.../TTiers('OID')` dont il faut extraire l'OID.
- Sources : PDF officiels wiki1000 *"Import d'écritures (API)"* et *"Creation d'un fournisseur (API)"*
  (exemples complets requête+réponse), + notes manuscrites de Valérie Caron (Mercuria, 2026-11-05).

## 3. Ce qui N'EST PAS confirmé (points ouverts à garder visibles)

- **Suppression/mise à jour d'une écriture déjà importée** : recherche faite sur le wiki1000 et
  autour (pages *"Ressource (sdata)"*, *"Interrogation des écritures (API)"*, forums Sage X3/50) —
  **aucune méthode API de suppression, modification ou contre-passation d'une écriture comptable
  posted n'existe nulle part dans la documentation**. Ce n'est probablement pas un trou de
  documentation mais une **limite structurelle** : une écriture comptable validée est en principe
  immuable en comptabilité française (un forum Sage X3 mentionne même un paramètre `FRADGI` qui
  interdit purement et simplement la suppression d'écritures pour une société française), la seule
  voie normale étant la contre-passation — qui existe côté Sage uniquement comme fonction UI
  ("Extournes automatiques", menu Traitements), pas comme endpoint API documenté. L'entité de
  lecture des écritures déjà posted (`gentiers/TEcriture` / `gentiers/TVueEcriture`, trouvée via
  *"Interrogation des écritures (API)"*) ne documente que des opérations de lecture (`GET`), aucun
  `PUT`/`DELETE`.
  **Conséquence pour §6.1/§6.2** : traiter ceci comme un blocage définitif, pas temporaire — pas la
  peine de prévoir une tentative d'appel API de suppression/mise à jour qui n'a selon toute
  vraisemblance aucune chance d'exister ; bloquer directement, sans appel, est le comportement
  correct (reste à confirmer avec Mercuria s'il existe malgré tout un mécanisme caché, mais rien
  dans la doc publique ne le suggère).
  **Nuance importante** : la page générale *"SData"* (https://frp.sage.fr/index.php/SData) décrit
  bien une syntaxe `PUT`/`DELETE` générique (`https://host/sdata/mypackage/myclass('mykey')`), mais
  c'est une description du **protocole** avec des placeholders (`mypackage`, `myclass`, `'mykey'`),
  pas une confirmation que Sage l'autorise concrètement sur les entités écriture. La syntaxe
  générique existant dans le protocole ne veut pas dire qu'une ressource précise l'accepte côté
  serveur — beaucoup de ressources SData sont des vues en lecture seule malgré la disponibilité
  syntaxique du `PUT`/`DELETE` (`TVueEcriture` est explicitement nommée comme une "vue"). Donc ni
  confirmation ni infirmation supplémentaire : à tester empiriquement (cf. §10.3).
- Code journal Sage, code compte d'équilibre, code établissement/dossier : propres à ce dossier
  MC2I, aucune valeur connue — à saisir en configuration une fois obtenues.
- **TODO** : réaligner les plans comptables Odoo et Sage (`compteGeneral` suppose aujourd'hui que
  `line.account_id.code` correspond tel quel à un compte général Sage existant, cf. §7.2) — et côté
  Sage, paramétrer quels comptes généraux sont "auxiliarisables" (acceptent un `compteAuxiliaire`,
  cf. `typeCompte: "X"` en §7.2), faute de quoi l'envoi échouera sur les comptes concernés.
- **Point bloquant découvert en bac à sable (2026-11-08) : notion de "société" manquante.** Le
  dossier Sage `MC2I` (= `sage_contract_code`) contient en réalité **plusieurs sociétés**
  (IHM Sage : Paramètres > Fichiers de base > Sociétés — `IDTO`, `MC2I`, `MC2IBELGIUM`, `MDEV`,
  `MGDE`, `MGIN`, `MINV`, `OGUR`, **`TSMN` = Tasmane**), chacune avec ses propres établissements et
  codes `typePiece`. Le token API actuel est rattaché par défaut à la société `MC2I` (homonyme du
  dossier, mais une société différente de `TSMN`) : c'est pour ça qu'une pièce avec
  `etablissement: "TSMN"` échoue ("l'établissement TSMN n'existe pas dans la société MC2I") alors
  que cet établissement existe bel et bien, rattaché à la société TSMN.
  Aucun champ "société"/`codeSociete` documenté dans les PDF officiels wiki1000 (écritures, tiers)
  — le ciblage de société semble être une propriété de l'interlocuteur/token API utilisé, pas un
  champ du payload : changer le `contrat` dans l'URL vers `TSMN` a d'ailleurs **créé automatiquement
  un nouvel "Interlocuteur user-api TSMN" pour la société TSMN** (effet de bord constaté, à
  vérifier avec Mercuria/le support Sage s'il faut le nettoyer). **À demander à Mercuria avant de
  poursuivre les tests en prod** : existe-t-il déjà un token API dédié à la société TSMN (sinon il
  faut en faire créer un proprement), et confirmer que `sage_api_token` doit être remplacé par
  celui-ci plutôt que le token actuel scopé sur MC2I.
- "Matricule" pour les particuliers : ni le champ Odoo, ni le champ Sage ne sont identifiés.
- Stratégie d'attribution d'un code à la création d'un nouveau tiers côté Sage : à définir (cf. §8).
- **`res.partner.external_auxiliary_code`** (`taz-common/models/res_partner.py`) : champ hérité de
  l'ancien processus — servait à identifier le tiers pour l'export mensuel manuel des factures
  clients vers le cabinet d'expertise comptable (Groupe AF), avant ce connecteur. Il fait
  manifestement doublon avec `sage_client_code` une fois ce connecteur en production. **TODO : une
  fois le fonctionnement du connecteur vérifié avec Groupe AF, confirmer que ce champ n'est plus
  utilisé par aucun processus externe, puis le supprimer** (avec la contrainte
  `check_external_auxiliary_code_consistency` et son entrée dans `PROTECTED_FIELD_LIST`, toutes deux
  dans `project_accounting`). Ne pas le supprimer avant cette confirmation : il peut encore être
  consommé par un export ou un processus externe à Odoo qu'on ne maîtrise pas depuis ce module.

---

## 4. Configuration

### 4.1 `account.journal` (surcharge)

| Champ | Type | Rôle |
|---|---|---|
| `sage_sync_date_start` | Date | Active l'export vers Sage pour ce journal **et** sert de seuil : seules les pièces dont la date de comptabilisation (`move.date`) est strictement postérieure à cette date sont concernées. Vide = journal non concerné par le connecteur. **Remplace le booléen d'activation initialement prévu** — la présence de la date est elle-même l'activation, pas de risque d'incohérence coche/date (cf. décision du 2026-11-xx). |
| `sage_sync_date_end` | Date | Optionnelle. Si renseignée, seules les pièces dont la date de comptabilisation est strictement antérieure à cette date restent concernées (permet de clore une fenêtre de synchro, ex. bascule vers un autre système). Vide = pas de borne haute. |
| `sage_journal_code` | Char | Code journal Sage (`codeJournal`). **Remplace** l'usage de `journal_id.code` tel quel — on ne suppose plus que le code journal Odoo correspond au code Sage. |

### 4.2 `res.company` (surcharge)

| Champ | Type | Rôle |
|---|---|---|
| `sage_api_endpoint` | Char | URL de base de l'API Sage (ex: `https://mc2i-compta-prd.em.cloud-by-sage.fr`). |
| `sage_api_token` | Char | Token Bearer. |
| `sage_contract_code` | Char | **Contrat SData** dans l'URL (`MC2I` dans `.../sdata/MC2I/...`). |
| `sage_etablissement_code` | Char | Valeur du champ `etablissement`, requis dans chaque ligne d'écriture (ex. officiel : `"ETS1-S01"`) — identifiant *comptable* interne à Sage, distinct du contrat ci-dessus. |
| `sage_compte_equilibre` | Char | **Laissé vide par défaut, par choix délibéré** (cf. décision ci-dessous) : champ de secours pour `CompteEquilibre`, à ne renseigner que si Mercuria confirme qu'il est structurellement obligatoire à l'appel API même pour des écritures déjà équilibrées. **Remonté au niveau société** (plutôt que par journal) si jamais renseigné : un seul lot peut ainsi mélanger plusieurs journaux dans une même session sans ambiguïté. |

**Décision : ne pas alimenter `CompteEquilibre` par défaut.** Les lignes qu'on envoie proviennent
directement du solde réel des `account.move.line` d'une facture *posted* — par construction Odoo,
elles sont déjà et toujours strictement équilibrées (débit = crédit). Un compte d'équilibre actif
absorberait silencieusement un écart en cas de bug de mapping (ex. une ligne oubliée, un problème
d'arrondi multi-devise) au lieu de le faire échouer bruyamment — **on préfère que ça plante** plutôt
que de masquer une erreur de construction de l'écriture. Laisser `sage_compte_equilibre` vide est
donc la position par défaut assumée, pas juste une valeur non encore renseignée.

**Point ouvert restant, uniquement technique** : si Mercuria confirme que le champ `CompteEquilibre`
est **obligatoire à l'appel** (la session d'import est refusée sans lui, indépendamment de
l'équilibre réel des lignes), il faudra trouver une autre solution qu'un compte fonctionnel
"attrape-tout" — ex. un compte dédié explicitement surveillé pour ne **jamais** recevoir de
mouvement réel (toute écriture dessus = alerte), plutôt qu'un vrai compte de suspense qui
absorberait les écarts sans que personne ne s'en aperçoive.

---

## 5. Modèle

### 5.1 `account.move` (surcharge)

| Champ | Type | Valeurs |
|---|---|---|
| `sage_sync_status` | Selection | `to_send` (À envoyer), `sent` (Envoyé), `to_update` (À mettre à jour). Vide/`False` = jamais concerné (journal non coché, ou pas encore validé). |

Pas de valeur `error` dans la sélection : un envoi en échec **ne change pas** le statut (la pièce
reste `to_send` et sera retentée au prochain déclencheur — prochaine validation d'une autre pièce
sur un journal activé, ou cron de rattrapage, cf. §6.1). C'est un choix délibéré pour rester fidèle
à la demande ("on log l'erreur et c'est tout"), mais il mérite d'être confirmé : sans état d'erreur
visible, un échec persistant est invisible dans l'UI tant que personne ne regarde les logs.

### 5.2 `res.partner` (surcharge)

| Champ | Type | Rôle |
|---|---|---|
| `sage_client_code` | `Char(readonly=True)` | "Code tiers Sage client" — code du rôle `TClient` sur Sage. |
| `sage_vendor_code` | `Char(readonly=True)` | "Code tiers Sage fournisseur" — code du rôle `TFournisseur` sur Sage. |

**`readonly=True` sur la définition du champ** (modèle), pas seulement un `readonly="1"` posé dans
telle ou telle vue — garantit qu'**aucune vue formulaire**, actuelle ou future, ne pourra jamais le
rendre éditable par erreur (vue qu'on oublierait de marquer readonly, vue tierce héritée, etc.).

**Attention, ce que `readonly=True` ne garantit PAS** : c'est une consigne d'affichage pour les vues
Odoo, pas une protection ORM — un `write()` direct par code serveur, un appel RPC, ou un import de
données peut toujours modifier le champ malgré `readonly=True` sur sa définition. Le code du
connecteur (cascade §8.1, création §8.2) s'appuie donc normalement sur un simple `write()` serveur
(pas de contournement nécessaire, le `readonly` ne le bloque pas).

**Protection ORM ajoutée : `sage_client_code` et `sage_vendor_code` dans `PROTECTED_FIELD_LIST`**
(`project_accounting/models/res_partner.py`, sur le même modèle que `l10n_fr_einvoicing_extra` qui
étend déjà cette liste) — une fois le tiers "protégé" (déjà facturé, cf.
`_precompute_protected`), seul un utilisateur du groupe `account.group_account_user` (ADV) peut
modifier ces deux champs, comme le reste des données comptables du tiers. `sage_connector` dépend
donc maintenant de `project_accounting`.

**Pas de conflit en pratique** : la recherche automatique (§8.0 point 1) se déclenche à la
validation d'une pièce comptable, qu'un non-ADV ne peut de toute façon pas confirmer — la question
d'un blocage par `PROTECTED_FIELD_LIST` sur cette écriture ne se pose donc pas.

Pas de champ "code tiers maître" distinct : avec la convention retenue en §8.2 (`411`/`401` +
racine métier), le `TTiers.code` d'un rôle est directement `sage_client_code` ou
`sage_vendor_code` — un même tiers Odoo peut correspondre à deux `TTiers` Sage différents selon le
rôle (observé en conditions réelles : `411TASMANE` et `401TASMANE` ne sont pas le même enregistrement
`TTiers`). `sage_client_code`/`sage_vendor_code` suffisent donc, pas besoin d'un troisième champ.

---

## 6. Faits générateurs

### 6.1 Validation d'un `account.move`

Déclenché sur l'action de validation (passage en `posted`).

0. **Portée** : le move n'est concerné que si **toutes** ces conditions sont vraies — sinon on ne
   fait rien du tout (pas de contrôle tiers, pas de changement de statut, comme si le connecteur
   n'existait pas pour cette pièce) :
   - `journal_id.sage_sync_date_start` est renseignée (journal actif, cf. §4.1) ;
   - `move.date` (date de comptabilisation, aussi utilisée pour `datePiece` en §7.2) est
     strictement postérieure à `sage_sync_date_start` ;
   - si `journal_id.sage_sync_date_end` est renseignée, `move.date` lui est strictement antérieure.
1. **Contrôle bloquant** : selon `move_type`,
   - facture/avoir **client** (`out_invoice`, `out_refund`) → `partner_id.sage_client_code` doit
     être renseigné ;
   - facture/avoir **fournisseur** (`in_invoice`, `in_refund`) → `partner_id.sage_vendor_code`
     doit être renseigné.
   Si le code manque, tenter d'abord la cascade de recherche automatique (§8.1, §8.0 point 1) pour
   le trouver silencieusement. Si elle ne trouve rien : lever une erreur bloquante (`UserError`) qui
   renvoie vers la fiche tiers (bouton manuel, §8.0 point 2) — la validation échoue, rien n'est
   envoyé. **Jamais** de wizard de création à ce stade (cf. §8.0).
2. **Statut** :
   - si `sage_sync_status` est vide → passer à `to_send` ;
   - si `sage_sync_status == 'sent'` → passer à `to_update`.
3. **Envoi groupé** (porte sur TOUTES les pièces en attente, pas seulement celle qu'on vient de
   valider) :
   - rechercher tous les `account.move` avec `sage_sync_status == 'to_send'` (tous journaux
     activés confondus) ;
   - construire une session d'import unique contenant les lignes (`account.move.line`) de
     *toutes* ces pièces ;
   - nommer la session `AAAAMMJJHHMMSSmmm` (date+heure+milliseconde) concaténé à un UUID, pour une
     unicité garantie. **Cet identifiant doit être strictement identique aux 3 endroits de la
     trame** : `Caption` de la session, `codeSessionInf` et `codeSessionSup` de l'exécution.
   - envoyer. Lire la réponse — **le lot n'est PAS tout-ou-rien** (correction suite à l'exemple
     officiel ajouté en §7.4 : l'exécution accepte/rejette chaque pièce indépendamment, même en
     `$batch` `atomic`, puisque l'exécution métier se fait après le `commit` de la transaction) :
     - parser les `$messages` de la ressource `EXEC` pièce par pièce (cf. §7.4 pour le format exact
       observé) ;
     - pièce mentionnée dans un message de succès ("Enregistrement pièce n° ...") → `sent` ;
     - pièce mentionnée dans un message d'erreur → reste `to_send`, erreur loguée pour cette pièce
       précisément (pas pour tout le lot) ;
     - pièce absente de tout message → reste `to_send` par prudence, avertissement logué (statut
       indéterminé, ne pas supposer un succès) ;
     - si l'appel échoue entièrement (erreur réseau, timeout, HTTP non-200 sur le batch lui-même) :
       aucune pièce ne change de statut, toutes restent `to_send`.
4. **Pièces en `to_update`** : rechercher les `account.move` avec `sage_sync_status == 'to_update'`.
   **Non implémenté dans cette spec** : aucune méthode API de mise à jour d'une écriture déjà
   importée n'existe (cf. §3 — vraisemblablement une limite structurelle, pas un trou de doc). Ne
   rien tenter automatiquement pour ces pièces : les laisser visibles en `to_update` pour
   traitement manuel (contre-passation + ré-import côté Sage par un humain).

**Cron de rattrapage** (reprise de la demande initiale du besoin, toujours valable) : un job
quotidien qui relance le même traitement du point 3 pour couvrir le cas où le dernier envoi en
échec n'aurait pas été retenté faute de nouvelle validation.

### 6.2 Suppression d'un `account.move` ou `account.move.line`

Sur `unlink()`, **avant** d'appeler `super().unlink()` :
- si `sage_sync_status in ('sent', 'to_update')` : **bloquer directement** (`UserError` explicite,
  ex. *"Cette écriture a déjà été envoyée à Sage et ne peut plus être supprimée ; il faut la
  contre-passer."*) — **sans tenter d'appel API** : cf. §3, aucune méthode de suppression n'existe
  vraisemblablement, un appel ne ferait qu'ajouter un aller-retour réseau pour un échec garanti.
- si la pièce n'a jamais été envoyée (`sage_sync_status` vide ou `to_send`) : suppression normale,
  rien à faire côté Sage.

---

## 6bis. Faits générateurs (`res.partner`)

Les 4 opérations CRUD, une par une.

### 6bis.1 Create

**Rien.** Aucune recherche, aucun appel API à la création d'un `res.partner`. La résolution du tiers
sur Sage (cascade §8.1, création §8.2) n'est déclenchée qu'à la validation de la **première**
écriture comptable concernant ce tiers qui doit être envoyée à Sage au regard du paramétrage du
journal (§8.0 point 1, §6.1 point 1) — jamais à la création du contact lui-même, qui peut très bien
exister dans Odoo sans jamais donner lieu à une facture sur un journal connecté à Sage.

### 6bis.2 Read

**Rien.** Mentionné pour mémoire (liste complète des 4 opérations CRUD) — aucune logique
particulière n'est nécessaire en lecture.

### 6bis.3 Write

**Rien.** `write()` ne déclenche aucun appel API, ni recherche (§8.1) ni mise à jour (`PUT`), même
sur un tiers déjà résolu côté Sage.

**Pourquoi pas de push automatique** : le même tiers Sage (même `TTiers`) peut être partagé entre
plusieurs entités du groupe (Groupe AF, mc2I, ...), chacune pouvant avoir sa propre vision à jour de
la fiche (adresse, raison sociale, SIREN...). Un `PUT` déclenché automatiquement depuis Odoo à
chaque modification locale écraserait silencieusement une donnée saisie entre-temps par une autre
entité, sans qu'aucun des deux systèmes ne soit réellement "propriétaire" du champ modifié — il n'y
a pas de règle fiable pour arbitrer entre les deux sources. Une fois un tiers résolu (code client et/
ou fournisseur renseigné), **Sage redevient donc la seule source de vérité pour la fiche tiers** ;
Odoo ne la met plus à jour après coup, seule la création initiale (§8.2) ou un rattachement manuel
(§8.0 point 2) écrivent sur `TTiers`.

**Conséquence : Odoo peut être désynchronisé d'une donnée changée côté Sage** (code tiers, raison
sociale, SIREN, adresse...) sans que rien ne le signale. Accepté pour l'instant, par symétrie avec
l'engagement réciproque de non-modification déjà posé pour les écritures (§1bis) : Odoo ne relit
jamais la fiche tiers après résolution, donc une divergence ne provoque aucun comportement incorrect
(l'écriture continue de référencer le même code tiers Sage, qui reste valide), seulement un
affichage local (nom, adresse...) potentiellement obsolète. **Point ouvert** : si cet écart doit un
jour être détecté ou corrigé, ce sera une évolution à spécifier séparément — pas traité ici. Options
envisageables, du plus léger au plus lourd :

1. **Rien (statu quo)** : on accepte la divergence silencieuse. Simple, mais aucune visibilité si un
   tiers change radicalement côté Sage (ex. renommage après fusion).
2. **Réimport périodique en lecture seule** (cron nocturne) : relire `caption`/adresse/SIRET/TVA des
   tiers déjà résolus côté Sage et les réécrire côté Odoo (sens inverse de l'ancien push). Risque
   symétrique inverse de celui qu'on vient d'éliminer : Odoo écraserait alors une modification locale
   faite entre-temps dans Odoo. À réserver aux champs considérés non modifiables dans Odoo une fois
   le tiers résolu (à imposer via `readonly` le cas échéant).
3. **Alerte de divergence sans réécriture** : cron qui compare périodiquement (nom, SIRET, TVA) et
   pose une activité/notification Odoo sur la fiche concernée en cas d'écart, sans toucher aux
   données. Pas de risque d'écrasement, mais demande un traitement manuel à chaque alerte — à
   dimensionner selon le volume de tiers partagés réellement concerné.
4. **Ne suivre qu'un sous-ensemble critique de champs** : par exemple seulement le code tiers Sage
   (`sage_client_code`/`sage_vendor_code`) et son existence (tiers supprimé/désactivé côté Sage) —
   les champs "cosmétiques" (adresse, nom) restent volontairement non surveillés, car c'est surtout
   une rupture du lien (code invalide) qui casserait réellement l'export d'écritures, pas une
   adresse obsolète. Combinable avec l'option 3 (alerte ciblée sur ces champs critiques uniquement).

Si un besoin de resynchronisation Odoo → Sage apparaît malgré tout, ce sera un bouton manuel explicite
(action volontaire de l'utilisateur), jamais un déclenchement implicite sur `write()`.

### 6bis.4 Unlink (delete)

**Ne devrait jamais arriver** : la politique est d'archiver les `res.partner`
(`active = False`), pas de les supprimer. Mais même si une suppression survenait malgré tout :
**aucun appel à l'API Sage** — ni tentative de suppression, ni blocage de la suppression Odoo. Motif
explicite : le tiers Sage (`TTiers`/`TClient`/`TFournisseur`) peut être référencé/utilisé par une
autre entité de l'alliance, pas exclusivement par ce `res.partner` Odoo précis — le supprimer côté
Sage pourrait casser autre chose en dehors du périmètre de ce connecteur. Donc : pas de surcharge
`unlink()` nécessaire côté connecteur pour `res.partner` (contrairement à `account.move`, où le
blocage est au contraire indispensable, cf. §6.2) — la suppression Odoo, si elle se produit, suit
son cours normal, sans aucune interaction avec Sage.

---

## 7. Mapping écriture (facture → `$batch`)

### 7.1 En-tête de session (`cptimport/tsessionimport`)

| Champ Sage | Source Odoo / valeur |
|---|---|
| `Caption` | `AAAAMMJJHHMMSSmmm` + UUID, généré au moment de l'envoi groupé (un seul par lot, pas par facture) |
| `CompteEquilibre` | **Omis par défaut** (cf. décision en §4.2 : on veut que ça plante si une écriture n'est pas équilibrée, pas qu'un compte d'équilibre absorbe l'écart en silence). Si jamais `company.sage_compte_equilibre` est renseigné (cas exceptionnel, Mercuria aurait confirmé le champ obligatoire à l'appel) : `{"$url": "TCompteGeneral(codeCompte eq '<company.sage_compte_equilibre>')"}`. |

### 7.2 Ligne d'écriture (`cptimport/timportecritures`), une par `account.move.line` au solde non nul

| Champ Sage | Source Odoo |
|---|---|
| `SessionImport` | `{"$uuid": "<uuid de la session dans ce $batch>"}` |
| `codeJournal` | `move.journal_id.sage_journal_code` |
| `numeroPiece` | `move.name` |
| `datePiece` | `move.date` |
| `dateEcheance` | `move.invoice_date_due` (ou `datePiece` si absent) |
| `compteGeneral` | `line.account_id.code` *(TODO : vérifier la correspondance avec le plan comptable Sage — réaligner les plans comptables Odoo/Sage si besoin, et paramétrer côté Sage quels comptes sont "auxiliarisables" (acceptent un `compteAuxiliaire`), cf. §3)* |
| `compteAuxiliaire` | selon `move.move_type` : `line.partner_id.sage_client_code` (client) ou `line.partner_id.sage_vendor_code` (fournisseur), **uniquement sur la ligne dont le compte est "collectif"** (`account_type` égal à `asset_receivable`/`liability_payable`) — **pas** simplement "porte un `partner_id`" : Odoo propage `partner_id` sur toutes les lignes d'une facture (y compris produit/TVA), pas seulement la ligne tiers ; envoyer `compteAuxiliaire` sur une ligne non collective fait échouer l'import côté Sage ("Le type de collectif Non du compte ... ne permet pas d'identifier le type du tiers", confirmé en bac à sable le 2026-11-08) |
| `typeCompte` | `"X"` si la ligne est sur un compte collectif (`account_type` ∈ `asset_receivable`/`liability_payable`), `"G"` sinon — toujours renseigné, quel que soit le compte |
| `sens` | `"D"` si `line.balance > 0`, `"C"` sinon |
| `montant` | `abs(line.balance)` |
| `montantEnDevise` | *(TODO multi-devise : distinct de `montant` si devise pièce ≠ devise société)* |
| `deviseEcriture` | `move.currency_id.name` |
| `deviseTenueCompte` | `move.company_id.currency_id.name` |
| `etablissement` | `move.company_id.sage_etablissement_code` (cf. §4.2) |
| `typeEcriture` | `"Brouillard"` (fixe, confirmé) |
| `typePiece` | **Client (`out_invoice`/`out_refund`)** : `"FC"` (facture client) — sauf si le tiers facturé est lui-même une société du groupe (`res.company.partner_id`), auquel cas `"FCI"` (facture client intragroupe) ; `"VA"` (avoir client) reste une hypothèse non confirmée, pas d'équivalent FCI confirmé pour un avoir intragroupe *(TODO)*. **Fournisseur (`in_invoice`/`in_refund`)** : voir liste complète et point ouvert ci-dessous. |
| `libelleEcriture` | `line.name` ou `move.ref` ou `move.name`, **tronqué à 128 caractères** (limite confirmée empiriquement sur le bac à sable par bissection le 2026-11-08 : au-delà, l'EXEC échoue avec un message générique "(TranBatch) Erreur de transaction :1" sans rapport avec le vrai contenu, aucune limite documentée officiellement — ni les retours à la ligne ni les caractères spéciaux ne sont en cause, uniquement la longueur) |
| `urlScanPiece` | URL de la fiche Odoo de la facture — cf. détail ci-dessous |

**TODO** : vérifier s'il faut ajouter un champ sur la ligne d'écriture portant la TVA (`line.tax_ids`)
pour lui donner un code de TVA côté Sage (type `codeTVA`/`compteTVA`, non identifié pour l'instant) —
actuellement aucun champ de ce type n'est envoyé dans le mapping ci-dessus.

**Types de pièce côté fournisseur, liste complète relevée sur l'IHM Sage (2026-11-08)** :

| Code | Libellé |
|---|---|
| `AF` | Avoir fournisseurs |
| `FF` | Factures fournisseurs |
| `FFI` | Factures fournisseurs intragroupe |
| `FFM` | Facture fournisseurs sans BAP |
| `FFS` | Fact. four. SST |
| `FFTVA` | Facture Fournisseur de TVA |
| `OD` | Opérations diverses |
| `REP` | Reprise Ciel |
| `TVA` | Pièce avec TVA |

**Implémenté** : `in_invoice` → `"FF"` (facture fournisseur) sauf si le tiers est lui-même une
société du groupe, auquel cas `"FFI"` (facture fournisseur intragroupe) — même logique que FC/FCI
côté client, via `_sage_is_intragroup_partner()`. `in_refund` → `"AF"` (avoir fournisseur).

**TODO à trancher avec Mercuria/MC2I** : un **avoir fournisseur intragroupe** doit-il porter le type
`"FFI"` (comme son équivalent facture) ou rester `"AF"` (comme un avoir fournisseur normal) ? Pas
d'équivalent "avoir intragroupe" dans la liste IHM ci-dessus, donc pas implémenté pour l'instant —
`in_refund` reste toujours `"AF"` quel que soit le tiers en attendant cette clarification. Reste
aussi à déterminer le critère de choix pour `FFM`/`FFS`/`FFTVA`, non identifié pour l'instant.
**Code journal confirmé** : `HA` (achats), cf. §9.

**`urlScanPiece`** (champ libre côté Sage, aucun contrôle de format — confirmé par Mercuria :
*"aucun contrôle champ libre"*) : trois options envisagées, **l'option A retenue par défaut pour
l'instant** :

- **(A, retenue) URL de la page Odoo de l'écriture**, ex.
  `https://tfp.tasmane.com/odoo/action-397/25816`. Avantage : toujours disponible, ne dépend
  d'aucune pièce jointe. Inconvénient : nécessite d'être connecté à Odoo pour l'ouvrir.
  **Implémentation recommandée** : ne pas construire l'URL `/odoo/action-<id>/<res_id>` à la main
  (dépend d'un ID d'action spécifique, fragile si le menu/l'action change) — utiliser plutôt la
  route générique d'Odoo `move.get_base_url() + '/mail/view?model=account.move&res_id=%s' % move.id`
  (route `/mail/view`, définie dans `mail_thread.py`, déjà utilisée par les notifications email
  d'Odoo pour pointer vers n'importe quel enregistrement sans connaître son action) — redirige vers
  la bonne vue après authentification, sans dépendre d'un numéro d'action particulier.
- **(B, écartée) URL du PDF de la pièce** (ex.
  `https://tfp.tasmane.com/web/content/4256?filename=...&unique=...`). Inconvénients qui l'écartent
  pour l'instant : erreur affichée si la personne n'est pas déjà connectée, la pièce jointe n'est
  pas toujours persistée pour les factures clients, et certains cas ont plusieurs PDF justificatifs
  (laquelle choisir ?).
- **(C, écartée) URL de stockage externe, hors Odoo** : pérenne même en cas d'abandon d'Odoo ou de
  changement de domaine — mais aucune solution de stockage externe n'est en place aujourd'hui, pas
  de quoi l'implémenter dans l'immédiat.

### 7.3 Exécution (`cptimportmouvement/TProcessusPlanifImport/Executer`)

| Champ Sage | Valeur |
|---|---|
| `$resource.codeSessionInf` | le même `Caption` que la session (§7.1), en toutes lettres |
| `$resource.codeSessionSup` | idem |
| `$resource.importerExterne` | `true` |
| `$` (sur cette opération) | `{"$transaction": ["commit"]}` |

### 7.4 Exemple complet

Facture `INV/2027/0001`, journal `VT` (Sage `sage_journal_code = "VTE"`), client `TOTAL` dont
`sage_client_code = "411TOTAL"`, 2 lignes : 100,00 € HT compte 706000, 100,00 € compte 411000
(TVA ignorée pour simplifier l'exemple). `Caption` de session = horodatage + `uuid4().hex` complet
(32 caractères), tel que généré par le code :

```json
POST {endpoint}/compta/server/sdata/MC2I/$batch
{
  "$": {"$transaction": ["atomic"]},
  "$resources": [
    {
      "$httpMethod": "POST",
      "$url": "cptimport/tsessionimport",
      "$uuid": "session",
      "Caption": "20270115143022017-8f3b2c1a9d4e4f6ab2c1e9d4e4f6ab2c"
    },
    {
      "$httpMethod": "POST",
      "$url": "cptimport/timportecritures",
      "SessionImport": {"$uuid": "session"},
      "codeJournal": "VTE",
      "numeroPiece": "INV/2027/0001",
      "datePiece": "2027-01-15T00:00:00.000Z",
      "dateEcheance": "2027-02-14T00:00:00.000Z",
      "compteGeneral": "411000",
      "compteAuxiliaire": "411TOTAL",
      "typeCompte": "X",
      "sens": "D",
      "montant": 100.0,
      "montantEnDevise": 100.0,
      "deviseEcriture": "EUR",
      "deviseTenueCompte": "EUR",
      "etablissement": "ETS1-S01",
      "typeEcriture": "Brouillard",
      "typePiece": "FC",
      "libelleEcriture": "INV/2027/0001",
      "urlScanPiece": "https://tfp.tasmane.com/mail/view?model=account.move&res_id=25816"
    },
    {
      "$httpMethod": "POST",
      "$url": "cptimport/timportecritures",
      "SessionImport": {"$uuid": "session"},
      "codeJournal": "VTE",
      "numeroPiece": "INV/2027/0001",
      "datePiece": "2027-01-15T00:00:00.000Z",
      "dateEcheance": "2027-02-14T00:00:00.000Z",
      "compteGeneral": "706000",
      "sens": "C",
      "montant": 100.0,
      "montantEnDevise": 100.0,
      "deviseEcriture": "EUR",
      "deviseTenueCompte": "EUR",
      "etablissement": "ETS1-S01",
      "typeEcriture": "Brouillard",
      "typePiece": "FC",
      "libelleEcriture": "INV/2027/0001",
      "urlScanPiece": "https://tfp.tasmane.com/mail/view?model=account.move&res_id=25816"
    },
    {
      "$": {"$transaction": ["commit"]},
      "$httpMethod": "EXEC",
      "$url": "cptimportmouvement/TProcessusPlanifImport/Executer",
      "$uuid": "processus",
      "$resource": {
        "codeSessionInf": "20270115143022017-8f3b2c1a9d4e4f6ab2c1e9d4e4f6ab2c",
        "codeSessionSup": "20270115143022017-8f3b2c1a9d4e4f6ab2c1e9d4e4f6ab2c",
        "importerExterne": true
      }
    }
  ]
}
```

Réponse (exemple officiel du PDF wiki1000, sur un lot de plusieurs pièces — reproduit tel quel pour
montrer la forme, pas spécifique à l'exemple `TOTAL` ci-dessus) :

```json
{
  "$baseUrl": "http://localhost:8080/sdata/l1000/-/-/",
  "$title": "$batch - -",
  "$resources": [
    { "$httpStatus": 201, "$httpMethod": "POST", "$httpMessage": "Created",
      "$location": ".../cptimport/-/TSessionImport('...')" },
    { "$httpStatus": 201, "$httpMethod": "POST", "$httpMessage": "Created",
      "$location": ".../cptimport/-/TImportEcritures('...')" },
    { "...": "une entrée 201 Created par ligne d'écriture ..." },
    {
      "$httpStatus": 200,
      "$httpMethod": "EXEC",
      "$httpMessage": "Executed",
      "$url": "cptimportmouvement/TProcessusPlanifImport/Executer",
      "$uuid": "processus",
      "result": 0,
      "$messages": [
        "Importation de la session \"Session import par API - 13\"",
        "Erreur d'importation : lignes 1-:1",
        "Le solde de la pièce AC2019/01-000186 doit être nul en devise de tenue, de reporting et en devise de transaction (Lot n°251).",
        "Enregistrement pièce n° AC2019/01-000187",
        "Enregistrement pièce n° AC2019/01-000188",
        "Enregistrement pièce n° AC2019/01-000189",
        "Enregistrement pièce n° AC2019/06-000046",
        "Traitement terminé."
      ]
    }
  ],
  "$batch": { "$httpStatus": 200, "$httpMessage": "OK", "$milliseconds": "2344,0533" }
}
```

**⚠️ Point de confusion fréquent à la lecture de cette réponse** : on voit `201 Created` pour
*chaque* ligne d'écriture, y compris celles de la pièce `AC2019/01-000186` qui finit pourtant
rejetée plus bas. Ce n'est pas contradictoire — ce sont **deux étapes distinctes, sur deux objets
différents** :

1. **Les `POST` (`201 Created`)** créent des lignes de **staging/brouillon d'import**
   (`TImportEcritures`) : du simple stockage de données brutes en attente. **Aucun contrôle
   comptable n'a lieu à ce stade** (pas de vérification d'équilibre, de compte valide, etc.) —
   d'où le succès systématique, y compris pour la pièce qui sera rejetée ensuite.
2. **L'`EXEC` qui suit** (`TProcessusPlanifImport/Executer`) est l'étape qui **relit** ces lignes
   stockées, les regroupe par pièce (`numeroPiece`), et **tente de les transformer en véritables
   écritures comptables**. C'est seulement à ce moment, et uniquement à ce moment, que le contrôle
   métier (équilibre, etc.) s'applique — pièce par pièce, reporté uniquement en texte dans
   `$messages` de cette seule opération, jamais via un `$httpStatus` par ligne.

Donc : `201 Created` = "j'ai bien stocké tes données brutes en brouillon", **pas** "ton écriture
comptable est passée". Le verdict réel (acceptée/rejetée) n'existe que dans `$messages` de l'`EXEC`.

**Ce que ça change pour le plan §6.1** : le `"$batch": {"$httpStatus": 200}` final ne reflète que la
réussite **technique** de l'appel HTTP (tout a été reçu et traité, y compris le stockage en
brouillon) — **pas** que toutes les pièces ont été comptabilisées. Dans cet exemple, la pièce
`AC2019/01-000186` est **rejetée** (solde non nul) à l'étape `EXEC`, alors que les 4 autres pièces
du même lot (`...-000187`, `...-000188`, `...-000189`, `...-000046`) sont **acceptées et
enregistrées**, dans le *même* appel `$batch` avec `"$transaction": ["atomic"]` au niveau racine.
Autrement dit : la directive `atomic` garantit l'atomicité de l'étape 1 (création des brouillons,
tous `POST`, tous `201`), mais **pas** celle du traitement métier de l'étape 2 (`EXEC`), qui
accepte/rejette **chaque pièce indépendamment** — cohérent avec le fait que l'exécution se fait
justement après avoir *commit* la transaction (`"$": {"$transaction": ["commit"]}` sur l'opération
`EXEC`, cf. §2), précisément parce qu'elle ne peut pas tourner sous une transaction encore ouverte.

**Conséquence sur §6.1, point 3** : on ne peut pas se contenter de regarder `$batch.$httpStatus`
(toujours 200 tant que la requête HTTP aboutit) ni traiter le lot comme tout-ou-rien. Il faut
**parser `$messages`** de la ressource `EXEC` et faire correspondre chaque pièce individuellement :
- une ligne `"Enregistrement pièce n° <numeroPiece>"` → la pièce correspondante (celle dont
  `move.name == <numeroPiece>`) passe à `sent` ;
- une pièce mentionnée dans un message d'erreur (`"Erreur d'importation : ... pièce <numeroPiece>
  ..."`) → reste `to_send`, erreur loguée pour cette pièce précisément (pas pour tout le lot) ;
- une pièce du lot qui n'apparaît dans **aucun** message (ni succès ni erreur) : cas à ne pas
  ignorer silencieusement — logguer un avertissement explicite ("pièce envoyée mais absente de la
  réponse, statut indéterminé"), la laisser `to_send` par prudence plutôt que de supposer un succès.

Le format exact de ces messages ("Enregistrement pièce n° X", "Erreur d'importation : ... pièce Y
...") est celui observé dans l'exemple officiel — à reconfirmer sur ce dossier MC2I avant de figer
le parsing (regex), la formulation pouvant varier selon la version/langue de l'installation.

---

## 7bis. Mapping tiers comptables (`res.partner` → `gentiers/TTiers`)

En miroir du §7, mais pour la création/mise à jour d'un tiers plutôt qu'une écriture. Structure
confirmée par le PDF officiel wiki1000 "Creation d'un fournisseur (API)" (cf. §2) — voir aussi §8.2
pour le `$batch` complet de création (session unique POST + EXEC `creerClient`/`creerFournisseur`).

### 7bis.1 Champs envoyés (création `POST` et mise à jour `PUT` — même payload)

| Champ Sage | Source Odoo | Remarque |
|---|---|---|
| `code` | voir §8.2 (convention `411`/`401` + racine métier) | Uniquement à la création — un `PUT` cible la ressource existante par son OID, pas par `code`, donc ce champ n'a plus à être renvoyé sur une mise à jour *(TODO : confirmer qu'un `PUT` partiel sans `code` est accepté, ou s'il faut le renvoyer identique)*. |
| `caption` | `partner.name` (personne morale) ou `"%s %s" % (partner.first_name, partner.name)` (personne physique, à confirmer — cf. `complete_name` dans `taz-common`) | |
| `sitesList[0].code` | `"PRINCIPAL"` (fixe) | Un seul site géré pour l'instant — pas de notion d'adresses de livraison/facturation distinctes côté Odoo dans ce mapping. |
| `sitesList[0].caption` | `partner.name` | |
| `sitesList[0].adresse.nomRueVoie` | `partner.street` | *(TODO : `street2` non mappé — un seul champ adresse côté Sage dans l'exemple officiel, à vérifier s'il existe un second champ pour une ligne d'adresse supplémentaire)* |
| `sitesList[0].adresse.ville` | `partner.city` | |
| `sitesList[0].adresse.codePostal` | `partner.zip` | |
| `sitesList[0].adresse.pays` | `{"$url": "TPays(code eq '<code pays Sage>')"}` | *(TODO : table de correspondance code pays Odoo (ISO alpha-2) → code pays Sage (`"FRF"` pour la France, confirmé par l'exemple officiel ; le reste à vérifier au cas par cas))* |
| `numeroTVACEE` | `partner.vat` (TVA intracom) | Confirmé accepté en écriture, au même endroit qu'en lecture (sur `TTiers`). |
| `codeSIRET` | `partner.siret` | Confirmé accepté en écriture, sur `TTiers` directement (comme en lecture). |

### 7bis.2 Exemple — création (`TOTAL`, rôle Client)

Déjà détaillé en §8.2 "Cas A" avec la trame `$batch` complète (POST `TTiers` + EXEC `creerClient`) et
sa réponse — pas dupliqué ici, voir directement §8.2.

### 7bis.3 Exemple — mise à jour (`PUT`, tiers déjà résolu)

**Non implémenté actuellement** (cf. §6bis.3 : `write()` ne pousse jamais vers Sage). Exemple
conservé à titre de référence pour une éventuelle évolution future (bouton manuel de
resynchronisation) : pas de `$batch` nécessaire pour une mise à jour, un simple `PUT` sur la
ressource existante suffirait :

```json
PUT {endpoint}/compta/server/sdata/MC2I/gentiers/TTiers('<OID_TTIERS_EXISTANT>')
{
  "caption": "TOTAL",
  "sitesList": [{
    "code": "PRINCIPAL",
    "caption": "TOTAL",
    "adresse": {
      "nomRueVoie": "2 Place Jean Millier",
      "ville": "Courbevoie",
      "codePostal": "92400",
      "pays": {"$url": "TPays(code eq 'FRF')"}
    }
  }]
}
```

**Non testé sur le bac à sable** (contrairement à la création, confirmée par le PDF officiel) — à
vérifier notamment : un `PUT` qui omet `code` est-il accepté tel quel, et le `sitesList` doit-il
porter le `$key` du site existant plutôt qu'être reconstruit à l'identique à chaque fois (risque de
dupliquer le site si Sage le traite comme une liste à remplacer plutôt qu'à fusionner) ?

---

## 8. Résolution et création des tiers

### 8.0 Déclencheurs

Deux points d'entrée distincts, avec des pouvoirs différents — **la création (§8.2) n'est jamais
déclenchée automatiquement pendant la validation d'une facture** : interrompre une validation (ou
pire, une validation de masse) par un wizard de confirmation serait une mauvaise expérience, et
l'exigence "jamais de création silencieuse" (§8.2) implique de toute façon un humain devant un
écran, pas un process de fond.

1. **Automatique, à la validation d'un `account.move`** (dans le contrôle bloquant du §6.1, point
   1) : si le code pertinent (`sage_client_code` ou `sage_vendor_code` selon `move_type`) est vide,
   lancer **uniquement la cascade de recherche (§8.1)**, silencieusement :
   - trouvé → remplir le champ, continuer la validation normalement ;
   - rien trouvé (ou nécessite une validation utilisateur, cf. critère 4 de §8.1) → **ne pas**
     proposer la création ici ; bloquer la validation avec un message explicite qui renvoie vers le
     bouton manuel (point 2), ex. *"Aucun code Sage trouvé pour ce tiers. Ouvrez sa fiche et utilisez
     le bouton 'Rechercher / créer sur Sage' avant de valider cette facture."*
2. **Manuel, bouton sur la fiche `res.partner`** ("Rechercher / créer sur Sage", ou deux boutons
   séparés recherche/création) : lance la cascade (§8.1) puis, si rien n'est trouvé, propose le
   wizard de création (§8.2) avec confirmation explicite de l'utilisateur. C'est le **seul** chemin
   qui peut aboutir à une création de tiers côté Sage.

Conséquence pratique : un utilisateur qui valide une facture pour un tiers jamais vu sur Sage doit
d'abord passer par sa fiche contact pour la résoudre/créer, *avant* de pouvoir valider — pas de
raccourci "créer à la volée" pendant la validation.

**Point structurant confirmé par les données réelles** : sur ce dossier, un même tiers "métier" peut
correspondre à **deux enregistrements `TTiers` Sage distincts** (OID différents), un par rôle,
chacun avec son propre `code` préfixé (`411TASMANE` pour le rôle client, `401TASMANE` pour le rôle
fournisseur — observé, pas une simple hypothèse). La cascade ci-dessous doit donc traiter les deux
rôles indépendamment, sans supposer un seul `TTiers` par tiers Odoo.

### 8.1 Cascade de recherche sur Sage

**Simplifiée suite à la correction ci-dessus** : pas besoin de chercher `TTiers` puis de résoudre
le rôle en 2 temps (ancienne §8.2, supprimée) — interroger **directement** `TClient` et
`TFournisseur` avec le critère disponible, par jointure vers `TTiers`. Chaque requête réussie donne
directement le code du rôle dans `Caption`.

Pour un `res.partner` donné, chercher dans cet ordre, **par rôle, indépendamment** — ne pas arrêter
toute la cascade dès qu'un rôle est trouvé, continuer les critères suivants pour le rôle encore
manquant (cf. bizarrerie de données en §2 : un SIRET absent sur un rôle ne veut pas dire que ce rôle
n'existe pas, juste que la donnée n'est pas renseignée dessus) :

- dès que `sage_client_code` est trouvé, arrêter la cascade *pour le rôle Client* uniquement ;
- dès que `sage_vendor_code` est trouvé, arrêter la cascade *pour le rôle Fournisseur* uniquement ;
- continuer à descendre les critères suivants pour le ou les rôles encore non résolus, jusqu'au
  dernier critère (nom) ;
- ce n'est qu'après avoir épuisé **tous** les critères sans résultat pour un rôle donné qu'on peut
  conclure "rôle introuvable" et passer à la création (§8.2) pour ce rôle précis.

Grille des critères, dans l'ordre :

| Ordre | Critère Odoo | Recherche Sage | Remarque |
|---|---|---|---|
| 1 | `partner.siret` | `gentiers/TClient(tiers.codeSIRET eq '<siret>')` et `gentiers/TFournisseur(tiers.codeSIRET eq '<siret>')` | Confirmé fonctionnel sur les deux rôles. Interroger les deux systématiquement (le tiers peut avoir l'un, l'autre, ou les deux — cf. remarque en tête de §8). |
| 2 | `partner.vat` (TVA intracom) | `gentiers/TClient(tiers.numeroTVACEE eq '<vat>')` et `gentiers/TFournisseur(tiers.numeroTVACEE eq '<vat>')` | Champ confirmé : `numeroTVACEE`. |
| 3 | "matricule" (particuliers) | — | **TODO** : ni le champ Odoo ni le champ Sage ne sont identifiés. À lister comme prérequis avant implémentation (poser la question à Mercuria : existe-t-il une recherche par identité pour les personnes physiques, type INSEE/NIR, ou autre identifiant Sage ?). |
| 4 | Nom — distinct selon `partner.is_company` | voir détail ci-dessous | Dernier recours, nom approximatif. **Validation utilisateur requise avant de rattacher automatiquement** (cf. §8.2) — un match par nom peut être un faux positif (homonymes, raisons sociales proches). |

**Détail du critère 4, selon `partner.is_company` :**

- **Personne morale** (`is_company = True`) : recherche sur la raison sociale —
  `gentiers/TClient(Tiers.raisonSociale eq '<partner.name>')` et
  `gentiers/TFournisseur(Tiers.raisonSociale eq '<partner.name>')`.
  *(remplace la recherche sur `caption` prévue initialement — `raisonSociale` est le champ métier
  pertinent pour une société, `caption` n'étant qu'un libellé d'affichage).*
- **Personne physique** (`is_company = False`) : recherche sur nom **et** prénom —
  `partner.name` (nom) et `partner.first_name` (prénom, champ déjà fourni par le module
  `taz-common`, cf. `taz-common/models/res_partner.py` — pas besoin d'installer de module
  supplémentaire ni de scinder `name` par heuristique, point levé). `sage_connector` devra déclarer
  `taz-common` en dépendance pour s'appuyer sur ce champ.
  `gentiers/TClient(Tiers.nom eq '<partner.name>' and Tiers.prenom eq '<partner.first_name>')`, idem
  `TFournisseur`. **TODO restant** : noms exacts des champs Sage (`nom`/`prenom` sont une hypothèse
  de travail, à confirmer avec Mercuria comme le reste de la cascade §8.1).

  **Limite connue et acceptée : sensibilité à la casse de la valeur comparée (2026-11-08).**
  Odoo stocke les raisons sociales en majuscules (ex. `"CHRISTIAN DIOR COUTURE"`), alors que Sage
  les enregistre en casse mixte (ex. `"Christian Dior Couture"`). Ni `eq` ni `like` ne sont
  insensibles à la casse côté Sage pour ce champ (les deux testés en bac à sable, aucun ne trouve le
  tiers si la casse diffère), et `tolower()`/`toupper()` ne sont pas supportés par le parseur
  (`"BadQueryParameter"`). Conséquence acceptée : ce critère de dernier recours échouera
  silencieusement chaque fois que la casse diffère entre Odoo et Sage — pas de correctif fiable côté
  client identifié (une normalisation type `.title()` serait trop fragile, ex. `"MC2I"` →
  `"Mc2I"`). Dans ce cas, l'utilisateur tombe sur l'option "créer un nouveau tiers" du wizard et peut
  saisir manuellement le bon code s'il le connaît déjà — cohérent avec le principe existant que ce
  critère reste de toute façon soumis à confirmation humaine (§8.1 critère 4), jamais automatique.
  (Le préfixe de jointure `Tiers.`/`tiers.`, lui, n'a aucun impact : les deux casses fonctionnent.)

Pour chaque rôle trouvé : `partner.sage_client_code = <Caption du TClient>` et/ou
`partner.sage_vendor_code = <Caption du TFournisseur>`, indépendamment l'un de l'autre.

**URLs d'appel** (exemple fictif, tiers `TOTAL`, SIRET et TVA inventés pour l'exemple) :

```
# Critère 1 — SIRET
GET {endpoint}/compta/server/sdata/MC2I/gentiers/TClient(tiers.codeSIRET eq '11122233344455')
GET {endpoint}/compta/server/sdata/MC2I/gentiers/TFournisseur(tiers.codeSIRET eq '11122233344455')

# Critère 2 — TVA intracommunautaire
GET {endpoint}/compta/server/sdata/MC2I/gentiers/TClient(tiers.numeroTVACEE eq 'FR00111222333')
GET {endpoint}/compta/server/sdata/MC2I/gentiers/TFournisseur(tiers.numeroTVACEE eq 'FR00111222333')

# Critère 4 — personne morale (raison sociale)
GET {endpoint}/compta/server/sdata/MC2I/gentiers/TClient(tiers.raisonSociale eq 'TOTAL')
GET {endpoint}/compta/server/sdata/MC2I/gentiers/TFournisseur(tiers.raisonSociale eq 'TOTAL')

# Critère 4 — personne physique (nom + prénom) — syntaxe "and" non confirmée, cf. TODO ci-dessus
GET {endpoint}/compta/server/sdata/MC2I/gentiers/TClient(tiers.nom eq 'Dupont' and tiers.prenom eq 'Jean')
GET {endpoint}/compta/server/sdata/MC2I/gentiers/TFournisseur(tiers.nom eq 'Dupont' and tiers.prenom eq 'Jean')
```

Pas d'URL pour le critère 3 (matricule) : champs Odoo et Sage non identifiés, cf. TODO dans le
tableau ci-dessus — rien à appeler tant que ce n'est pas levé.

### 8.1bis Pagination des résultats de recherche

Une réponse de collection SData peut porter un `$nextUrl` quand tous les résultats ne tiennent pas
dans une seule page (confirmé présent sur d'autres vues de ce dossier, cf. réponse facture en §2).
À traiter pour **chaque appel** de la cascade §8.1 :

- tant que la réponse contient un `$nextUrl` non vide, rappeler cette URL (déjà complète, pas besoin
  de reconstruire les paramètres) et accumuler les `$resources` de chaque page ;
- **plafond de sécurité** : arrêter après un nombre maximal de pages (ex. 20) même si `$nextUrl` est
  encore présent, et logger un avertissement explicite plutôt que de boucler indéfiniment — une
  volumétrie anormalement élevée sur une recherche censée être ciblée (SIRET, TVA, nom) est le signe
  d'un critère de recherche trop large ou d'un problème côté Sage, pas une situation à gérer en
  silence jusqu'au bout ;
- **tous** les résultats accumulés (pas seulement ceux de la première page) comptent pour décider
  s'il y a ambiguïté (ex. critère 4, recherche par nom : plusieurs résultats sur l'ensemble des pages
  → validation utilisateur requise, pas seulement si plusieurs résultats tiennent sur la première
  page) ;
- en pratique, les critères 1 et 2 (SIRET, TVA intracom) ne devraient renvoyer qu'un résultat au
  plus par rôle — la pagination y sert surtout de garde-fou si jamais des doublons existent côté
  Sage. Le critère 4 (nom) est celui le plus susceptible de déclencher une pagination réelle.

### 8.2 Tiers introuvable sur Sage

- Demander confirmation à l'utilisateur (wizard) avant toute création — jamais de création
  automatique silencieuse.
- Si confirmé : attribuer un code et créer via le `$batch` POST `TTiers` + EXEC `creerClient` (ou
  `creerFournisseur` selon le rôle manquant — le rôle à créer est déterminé par le contexte qui a
  déclenché la recherche, ex. `move_type` de la facture en cours de validation : client pour
  `out_invoice`/`out_refund`, fournisseur pour `in_invoice`/`in_refund`). Un même tiers Odoo peut
  nécessiter les deux créations séparément, à des moments différents, s'il finit par être à la fois
  client et fournisseur (deux `TTiers` indépendants, cf. §8.1-8.2).
- **Attribution du code** : convention déjà observée et retenue sur ce dossier MC2I — le code
  `TTiers` est construit comme `<racine du compte de contrôle><racine métier>` :
  - rôle **client** → préfixe `411` ;
  - rôle **fournisseur** → préfixe `401` ;
  - `<racine métier>` = nom du tiers normalisé (majuscules, sans accents/espaces/caractères
    spéciaux), ex. `TASMANE` pour "Tasmane". **TODO** : longueur maximale du champ `code` côté Sage
    non confirmée (les exemples observés sont courts, ex. `411TASMANE` = 10 caractères) — tronquer
    si nécessaire, règle de troncature à définir une fois la limite réelle connue.
  - Un même tiers Odoo peut donc avoir deux codes différents selon le rôle créé (`411TASMANE` côté
    client, `401TASMANE` côté fournisseur), cohérent avec `sage_client_code`/`sage_vendor_code`
    distincts (§5.2) — pas de champ `sage_tiers_code` unique commun aux deux rôles dans ce cas.
  - **Point ouvert à poser à Mercuria avant de coder la création** : cette duplication (deux
    `TTiers` séparés par rôle) ressemble à une pratique historique propre à ce dossier plutôt qu'à
    une contrainte du modèle Sage, qui supporte nativement plusieurs rôles sur un seul `TTiers`
    (`RoleTiersList`, `creerClient`/`creerFournisseur` enchaînables sur le même `$uuid` dans
    l'exemple officiel §2). Faut-il reproduire cette duplication à la création d'un nouveau tiers,
    ou créer un seul `TTiers` et y ajouter les deux rôles au besoin ? Ça change le comportement de
    §8.1-8.2 quand un tiers déjà connu côté Sage (un seul rôle) a besoin du second rôle plus tard :
    créer un second `TTiers`, ou ajouter le rôle manquant sur le `TTiers` existant ?

**Trames d'appel** (tiers `TOTAL`, adresse fictive pour l'exemple ; structure reprise à l'identique
de l'exemple officiel wiki1000 "Creation d'un fournisseur (API)", cf. §2) :

#### Cas A — nouveau tiers, création du rôle Client (`out_invoice`/`out_refund`)

Requête :
```json
POST {endpoint}/compta/server/sdata/MC2I/$batch
{
  "$": {"$transaction": ["atomic"]},
  "$resources": [
    {
      "$httpMethod": "POST",
      "$url": "gentiers/TTiers",
      "$uuid": "newTiers",
      "$shareLevel": 2,
      "code": "411TOTAL",
      "caption": "TOTAL",
      "sitesList": [{
        "code": "PRINCIPAL",
        "caption": "TOTAL",
        "adresse": {
          "nomRueVoie": "2 Place Jean Millier",
          "ville": "Courbevoie",
          "codePostal": "92400",
          "pays": {"$url": "TPays(code eq 'FRF')"}
        }
      }]
    },
    {
      "$httpMethod": "EXEC",
      "$url": "gentiers/TTiers(newTiers)/creerClient"
    }
  ]
}
```

Réponse (forme confirmée par l'exemple officiel — pas de `$key`, OID à extraire de `$location`) :
```json
{
  "$baseUrl": "{endpoint}/compta/server/sdata/MC2I/-/-/",
  "$title": "$batch - -",
  "$resources": [
    {
      "$httpStatus": 201, "$httpMethod": "POST", "$httpMessage": "Created",
      "$location": "{endpoint}/compta/server/sdata/MC2I/gentiers/-/TTiers('<OID_TTIERS>')"
    },
    {
      "$httpStatus": 200, "$httpMethod": "EXEC", "$httpMessage": "Executed",
      "$url": "gentiers/TTiers(newTiers)/creerClient", "$uuid": ""
    }
  ],
  "$batch": {"$httpStatus": 200, "$httpMessage": "OK"}
}
```

**Important** : ne pas supposer que `sage_client_code = "411TOTAL"` (le code envoyé) — l'exemple
officiel montre Sage **re-préfixant** le code à la création du rôle fournisseur (`"NV-TIERS-1"` →
`"F-NV-TIERS-1"`, cf. §2). Après ce `$batch`, **relire** le `TClient` créé (`GET
.../TClient(tiers.code eq '411TOTAL')`, cf. §8.1) pour récupérer son `Caption` réel et le stocker
dans `sage_client_code` — même si la convention 411/401 (ci-dessus) laisse penser qu'il n'y aura pas
de re-préfixage côté Client, ne pas le tenir pour acquis sans vérification.

#### Cas B — nouveau tiers, création du rôle Fournisseur (`in_invoice`/`in_refund`)

Identique au cas A, avec `"code": "401TOTAL"` et `"$url": "gentiers/TTiers(newTiers)/creerFournisseur"`
à la place de `creerClient`. Même remarque sur la relecture du `Caption` réel après création.

#### Cas C — tiers déjà connu côté Sage avec un seul rôle, second rôle manquant

**Hypothèse non testée**, dépend de la réponse de Mercuria au point ouvert ci-dessus. Si la réponse
est "ajouter le rôle manquant sur le `TTiers` existant" (pas de duplication) :

```json
POST {endpoint}/compta/server/sdata/MC2I/$batch
{
  "$": {"$transaction": ["atomic"]},
  "$resources": [
    {
      "$httpMethod": "EXEC",
      "$url": "gentiers/TTiers('<OID_TTIERS_EXISTANT>')/creerFournisseur"
    }
  ]
}
```
Réponse attendue par analogie avec le cas A/B : un seul élément dans `$resources`, `$httpStatus: 200`,
`$httpMethod: EXEC`. **Non vérifié sur le bac à sable** — à tester avant d'implémenter cette branche.

---

## 9. Ce que cette spec laisse volontairement de côté

- Export des factures/avoirs **fournisseurs** : le code le supporte déjà structurellement de bout
  en bout (`SAGE_DOCUMENT_TYPE_BY_MOVE_TYPE` et `SAGE_PARTNER_CODE_FIELD_BY_MOVE_TYPE` dans
  `account_move.py`, `_sage_create_role(role='vendor')` dans `res_partner.py`) — ce n'est donc pas
  une fonctionnalité manquante. Il reste simplement inactif tant qu'aucun journal d'achat n'a de
  `sage_sync_date_start` renseigné (§6.1 point 0) : l'activation est une question de configuration,
  à faire quand le besoin fournisseurs sera confirmé. **Code journal confirmé : `HA`** (`sage_journal_code`
  du journal d'achat Odoo). Cf. §7.2 pour la liste des `typePiece` fournisseur et le point ouvert sur
  lequel utiliser selon le cas.
- Interface (vues, wizard de création de tiers, menu de supervision des pièces en erreur) : pas
  détaillée dans cette spec technique — à faire une fois le mapping validé.
- Détection automatique de divergence entre l'écriture envoyée et son état réel côté Sage (cf.
  §1bis) : cette spec part du principe que l'engagement réciproque (aucune modification/suppression
  manuelle côté Sage) est respecté, et ne prévoit aucune vérification de réconciliation périodique.
  À ajouter en évolution si cet engagement s'avère insuffisant en pratique.

---

## 10. Vérifications à faire sur le bac à sable avant mise en prod

Des tests manuels à exécuter avant de faire confiance à la logique de retry/statut, en plus des
points déjà marqués TODO plus haut. À compléter au fil des découvertes.

### 10.1 Doublon de `numeroPiece`

**Risque identifié** : le design de retry (§6.1 point 3) laisse une pièce en `to_send` tant qu'elle
n'apparaît pas clairement dans un message de succès — si un envoi réussit réellement côté Sage mais
que la réponse est perdue/ambiguë côté Odoo (coupure réseau après l'appel, timeout, etc.), le
prochain cycle va **renvoyer la même pièce une seconde fois**, avec le même `numeroPiece`. D'après
Mercuria, `numeroPiece` est censé être unique dans la base Sage — mais il faut vérifier
concrètement ce qui se passe, pas le supposer :

- Envoyer un `$batch` complet (session + lignes + exécution) pour une pièce `numeroPiece` donné,
  vérifier qu'elle est acceptée (`"Enregistrement pièce n° ..."` dans `$messages`).
- Renvoyer **exactement le même batch** une seconde fois (même `numeroPiece`, nouvelle session avec
  un `Caption` différent comme il se doit) et vérifier :
  - que Sage **rejette** la seconde tentative (message d'erreur explicite, pas un doublon silencieux
    de l'écriture comptable) ;
  - que ce message de rejet est bien formulé de façon à être détecté par le parsing prévu en §6.1/§7.4
    (contient "Erreur", mentionne le `numeroPiece` concerné) — sinon notre détection pièce-par-pièce
    laisserait passer un faux succès ;
  - qu'aucune écriture en double n'apparaît côté Sage (vérifier par lecture, ex. `gentiers/tecriture`
    ou `tvueecriture`, cf. §3) après les deux tentatives.

**Si le résultat diffère de "rejet propre et détectable"** (ex: doublon silencieusement créé, ou
message de rejet qui ne contient pas "Erreur" et serait donc mal interprété comme un succès par notre
parsing) : le design du §6.1 doit être revu avant la mise en prod — soit en changeant la détection de
succès/échec, soit en ajoutant un garde-fou côté Odoo (ex: vérifier qu'on n'a jamais déjà envoyé ce
`numeroPiece` avant de retenter, indépendamment du statut `sage_sync_status`).

### 10.2 Rejet d'une écriture déséquilibrée sans `CompteEquilibre`

**Vérifie la décision du §4.2/§7.1** (ne pas renseigner `CompteEquilibre`, pour que Sage rejette
toute écriture mal équilibrée plutôt que d'absorber l'écart en silence) :

- Envoyer une session d'import **sans** `CompteEquilibre`, avec des lignes **volontairement
  déséquilibrées** (débit ≠ crédit de quelques centimes) — vérifier que l'exécution rejette
  bien la pièce (message d'erreur type "solde non nul", comme dans l'exemple officiel du §7.4),
  et confirmer qu'aucune écriture n'est créée côté Sage malgré l'absence de compte d'équilibre.
- Envoyer une session **normale, équilibrée, toujours sans `CompteEquilibre`** — confirmer que
  l'absence du champ n'empêche pas l'import d'une pièce correcte (sinon le champ serait
  structurellement obligatoire, cf. point ouvert en §4.2, et il faudrait revoir la décision).

### 10.3 PUT/DELETE sur une écriture déjà posted

**Vérifie la nuance du §3** sur la page générale *"SData"* (syntaxe `PUT`/`DELETE` générique au
protocole, pas forcément autorisée par Sage sur les entités écriture réelles) :

- Tenter un `DELETE` sur une entrée `gentiers/TEcriture('<clé>')` (ou `TVueEcriture` si c'est la
  seule accessible en lecture) pointant vers une écriture déjà posted de test — s'attendre à un
  refus (403/405, ou message métier explicite), mais le confirmer plutôt que de le supposer.
- Idem avec un `PUT` (tentative de modification d'un champ anodin, ex. `libelleEcriture`).
- **Si l'un des deux réussit contre toute attente** : réviser entièrement §6.1 point 4
  (`to_update`) et §6.2 (blocage suppression) — ça rouvrirait une voie qu'on pensait fermée. Document
  à mettre à jour en priorité si ce test donne un résultat inattendu.
- Cas limite à tester séparément si le temps le permet : `DELETE`/`PUT` sur une ligne
  `cptimport/timportecritures` **avant** l'étape `EXEC` (donc encore en brouillard, pas encore
  posted) — plausible que ce soit permis à ce stade puisque rien n'est encore comptabilisé, même si
  notre design actuel exécute toujours dans le même `$batch` et ne laisse jamais de ligne en
  brouillard non exécutée volontairement.

### 10.4 `PUT` de mise à jour d'un tiers — risque de doublon de site

**Vérifie le point ouvert du §7bis.3** : envoyer un `PUT` de mise à jour sur un `TTiers` déjà créé
(ex. le tiers `TOTAL` créé lors du test du §8.2), avec un `sitesList` reconstruit à l'identique
(même `code`: `"PRINCIPAL"`, adresse modifiée), puis **relire le tiers** pour vérifier :

- que `sitesList` ne contient toujours qu'**un seul** site après le `PUT` (pas de doublon créé parce
  que Sage aurait traité la liste comme un ajout plutôt qu'un remplacement/fusion par `code`) ;
- que l'adresse a bien été mise à jour (pas restée sur l'ancienne valeur, auquel cas le `PUT` aurait
  été silencieusement ignoré sur ce sous-objet).

**Si un doublon apparaît** : il faudra probablement référencer le site existant par son `$key` (OID)
plutôt que de renvoyer `sitesList` à l'identique à chaque mise à jour — à relire depuis le `TTiers`
existant avant de construire le payload du `PUT`, plutôt que de reconstruire `sitesList` depuis zéro
à partir des seules données Odoo.

---

## 11. Primo-configuration à la première installation

Quatre choses à paramétrer avant que le connecteur ne devienne actif sur une base.

### 11.1 Société (`res.company`)

Menu **Comptabilité/Facturation → Configuration → Paramètres**, onglet "Sage FRP 1000" (ou
directement sur la fiche société, cf. §4.2) :

| Champ | Valeur |
|---|---|
| `sage_api_endpoint` | URL de base de l'API Sage (ex. `https://mc2i-compta-prd.em.cloud-by-sage.fr`). |
| `sage_api_token` | Le token Bearer. |
| `sage_contract_code` | Le contrat SData dans l'URL (ex. `MC2I`). |
| `sage_etablissement_code` | L'identifiant comptable Sage (ex. `"ETS1-S01"`) — à ne pas confondre avec le contrat ci-dessus. |
| `sage_compte_equilibre` | **À laisser vide** sauf si Mercuria confirme qu'il est obligatoire côté Sage (cf. décision §4.2). |

### 11.2 Journaux (`account.journal`, un par journal à activer)

| Champ | Rôle |
|---|---|
| `sage_sync_date_start` | **C'est ce champ qui active réellement l'export** pour ce journal (vide = journal ignoré) ; seules les pièces dont `move.date` est strictement postérieure à cette date seront envoyées. Choisir une date de bascule (ex. la veille du premier jour où on veut commencer à envoyer). |
| `sage_journal_code` | Le code journal côté Sage (`codeJournal`) — ne pas supposer qu'il est identique au code journal Odoo. |
| `sage_sync_date_end` | Optionnel, seulement si on veut fermer la fenêtre d'envoi plus tard. |

Au départ, ne configurer que les journaux **ventes** (`out_invoice`/`out_refund`) — les journaux
achats sont structurellement supportés mais le besoin initial reste focalisé clients (cf. §9).

### 11.3 Tiers déjà facturés historiquement

Rien à faire en masse : la résolution (recherche SIRET/TVA puis bouton manuel si besoin) se
déclenche au fil de l'eau, à la validation de chaque pièce concernée (§8.0).

### 11.4 Vérification du cron

`ir_cron_sage_connector_process_pending` est actif par défaut (rattrapage quotidien des pièces
restées `to_send`) — rien à activer manuellement, juste à surveiller les premiers jours.

### 11.5 Avant la mise en prod

Il reste aussi les vérifications manuelles sur le bac à sable listées au §10 (doublon de pièce,
atomicité du batch, etc.) à faire une fois, indépendamment du paramétrage ci-dessus.
