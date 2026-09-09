# 26 — Validation des capabilities sur iPhone physique

> **MANUAL VALIDATION REQUIRED — v0.11:** ce document est un protocole de
> validation, pas une preuve d'exécution. Aucun des scénarios ci-dessous n'est
> marqué réussi tant qu'il n'a pas été observé sur un iPhone physique avec le
> build, la version iOS et les identifiants de preuve consignés. Les tests
> unitaires, le typecheck et un build signé ne remplacent pas cette validation.

> **Tentative du 2026-09-08: BLOCKED — guest transport missing.** Le diagnostic
> a identifié un guest QEMU sans iPhone dans l'énumération USB; les appareils
> connus de Xcode étaient seulement des entrées en cache hors ligne, avec tunnel
> CoreDevice non connecté et services DDI indisponibles. Aucun scénario natif
> n'a donc été exécuté. Voir
> `docs/evidence/iphone-validation-2026-09-08.md`.

## Portée

Ce parcours valide la frontière réelle entre le grant autoritatif Ubuntu, le
broker mobile et les API iOS pour:

- `iphone.location.current`;
- `iphone.contacts.lookup`;
- `iphone.calendar.events`;
- `iphone.photos.pick`;
- `iphone.mail.compose`;
- `iphone.sms.compose`.

Le principe reste: le serveur émet un grant court lié aux arguments exacts,
l'iPhone le consomme avant l'effet natif, puis rapporte une issue corrélée. Une
notification WebSocket, la replica locale ou la présence d'une permission iOS
ne constitue jamais une autorisation d'exécuter.

## Prérequis et fiche de session

Utiliser uniquement des contacts, calendriers, photos, destinataires et textes
de test. Les captures et journaux ne doivent contenir ni bearer, ni grant, ni
coordonnées, ni fiche contact, ni contenu de mail/SMS.

```text
Résultat global: NOT RUN
Date/heure:
Opérateur:
Modèle iPhone:
Version iOS:
Source du build: développement signé | TestFlight
Version/build de l'app:
Commit:
Version du control plane:
Instance Ubuntu:
Origine HTTPS jumelée:
Réseau utilisé:
Identifiants de requête expurgés:
Lien vers les preuves expurgées:
```

Avant chaque cas:

1. créer une nouvelle demande de capability depuis une job et une lease actives;
2. relever seulement `request_id`, capability, statut et horodatages;
3. vérifier l'approbation visible sur l'iPhone ciblé;
4. ne jamais copier le grant ou sa valeur native dans la preuve partagée;
5. comparer l'état REST autoritatif, l'audit expurgé et l'UI mobile;
6. utiliser une nouvelle demande pour le cas suivant, sans réutiliser un grant.

Pour tester une première permission, supprimer l'autorisation dans Réglages iOS
ou réinstaller le build seulement si cette opération est prévue dans la fiche
de session. Noter l'état initial; ne pas conclure à l'absence de dialogue si la
permission avait déjà été accordée.

## Invariants communs à prouver

- Le dialogue ou composeur natif n'apparaît qu'après l'approbation et la
  consommation serveur du grant.
- Un grant expiré, consommé, lié à une autre requête ou à un autre appareil est
  refusé avant toute API iOS.
- Une livraison WebSocket en double ne déclenche pas un second effet natif.
- Un re-pair pendant qu'un WebSocket est établi ferme l'ancienne lignée de
  session; elle ne reçoit plus de demande et ne peut pas créer un nouveau
  ticket avec son ancien bearer. La nouvelle session refait un bootstrap REST.
- Après passage en arrière-plan puis reprise, l'app relit l'état autoritatif et
  ne déduit aucun droit de la replica.
- Si le réseau tombe après l'effet natif, une reprise du POST de résultat dans
  le même processus peut rapporter la même issue, mais ne réexécute pas l'API
  iOS. Après perte du processus, l'issue reste incertaine; aucune reprise
  automatique de l'effet n'est permise.
- Mail et SMS ouvrent un composeur visible. La présentation du composeur ou son
  résultat ne prouve ni livraison ni envoi silencieux.

## `iphone.location.current`

| Cas manuel | Procédure | Résultat attendu et preuve |
|---|---|---|
| Dialogue de permission | Partir de l'état Location «jamais demandé», approuver le grant, puis exécuter. | Le dialogue iOS foreground apparaît après consommation du grant; aucun point GPS n'est diffusé sur WebSocket, board ou audit générique. |
| Refus | Choisir «Ne pas autoriser». | Résultat `denied` avec raison publique bornée; aucune coordonnée n'est enregistrée comme succès. |
| Annulation | Vérifier le comportement disponible sur cette version iOS. Location n'offre normalement pas une issue «cancel» distincte du refus. | Consigner `N/A` si aucun geste d'annulation natif n'existe; ne jamais convertir un refus, une interruption ou une absence de valeur en succès. |
| Succès | Autoriser une acquisition ponctuelle au premier plan. | Une valeur conforme est visible seulement dans l'état autoritatif destiné à la job; la preuve partagée masque latitude, longitude et précision. |
| Arrière-plan/reprise | Mettre l'app en arrière-plan pendant le dialogue ou l'acquisition, puis revenir. | L'issue est unique et cohérente; aucune seconde acquisition automatique au retour. |
| Expiration | Laisser expirer le grant avant `execute`. | Refus serveur avant l'appel Location; aucun dialogue et aucune acquisition. |
| Perte réseau après effet | Couper le réseau immédiatement après l'acquisition, avant le POST de résultat, puis le rétablir. | La même issue peut être resoumise sans nouvelle acquisition dans le même processus; l'UI signale l'attente ou l'incertitude. |
| Aucun rejeu | Redélivrer la notification et redémarrer/reconnecter l'app après une issue terminale. | Aucun nouveau dialogue ni point GPS; l'état terminal est relu par REST. |

```text
Résultat location: NOT RUN
Permission initiale:
Issue observée par cas:
request_id expurgés:
Preuves:
Anomalies:
```

## `iphone.contacts.lookup`

| Cas manuel | Procédure | Résultat attendu et preuve |
|---|---|---|
| Dialogue de permission | Partir de l'état Contacts «jamais demandé», utiliser une requête visant une fiche de test. | Le dialogue iOS apparaît après consommation du grant; la requête brute et les coordonnées ne sont pas publiées sur les transports génériques. |
| Refus | Refuser l'accès Contacts. | Résultat `denied`; aucune fiche ou coordonnée n'est retournée. |
| Annulation | Vérifier l'API utilisée après la permission. La recherche programmatique n'offre normalement pas de picker annulable. | Consigner `N/A` si aucun UI natif d'annulation n'existe; une interruption ne produit jamais une liste vide présentée comme succès. |
| Succès | Autoriser et chercher uniquement la fiche de test. | Au plus 25 résultats et champs minimaux; la preuve masque noms, numéros et adresses. |
| Arrière-plan/reprise | Quitter puis reprendre l'app autour du dialogue ou de la lecture. | Une seule lecture corrélée; reprise depuis l'état serveur, sans relance implicite. |
| Expiration | Laisser expirer le grant avant l'exécution. | Aucun prompt Contacts ni lecture; statut d'expiration/refus cohérent côté serveur. |
| Perte réseau après effet | Couper le réseau après la lecture, avant le résultat, puis reconnecter. | Resoumission du même résultat en mémoire permise; aucune seconde lecture automatique. |
| Aucun rejeu | Injecter une notification dupliquée et reconnecter après issue terminale. | Aucun nouveau prompt ni accès Contacts; le résultat reste accessible seulement à la job autorisée. |

```text
Résultat contacts: NOT RUN
Permission initiale:
Issue observée par cas:
request_id expurgés:
Preuves:
Anomalies:
```

## `iphone.calendar.events`

| Cas manuel | Procédure | Résultat attendu et preuve |
|---|---|---|
| Dialogue de permission | Partir de l'état Calendrier «jamais demandé» avec une fenêtre bornée contenant un événement de test. | Le dialogue iOS apparaît après consommation; dates, titres et notes ne quittent pas l'API autoritative ciblée. |
| Refus | Refuser l'accès Calendrier. | Résultat `denied`; aucun événement n'est retourné. |
| Annulation | Vérifier l'API de lecture utilisée, qui n'offre normalement pas de picker annulable. | Consigner `N/A` si aucun geste natif n'existe; ne pas transformer une interruption en succès. |
| Succès | Autoriser la fenêtre de test. | Au plus 100 événements dans la fenêtre exacte; les preuves masquent tout contenu d'événement. |
| Arrière-plan/reprise | Passer en arrière-plan autour du dialogue/de la lecture, puis revenir. | Une seule lecture; pas de nouvelle requête sans action explicite. |
| Expiration | Exécuter après expiration du grant. | Refus avant Calendar; aucun dialogue et aucune lecture. |
| Perte réseau après effet | Couper le réseau après lecture et avant soumission du résultat. | Même résultat resoumis dans le processus; aucune seconde lecture automatique. |
| Aucun rejeu | Dupliquer la notification, reconnecter et rouvrir l'app. | Aucun second accès Calendar; terminalité relue depuis Ubuntu. |

```text
Résultat calendrier: NOT RUN
Permission initiale:
Issue observée par cas:
request_id expurgés:
Preuves:
Anomalies:
```

## `iphone.photos.pick`

| Cas manuel | Procédure | Résultat attendu et preuve |
|---|---|---|
| Dialogue de permission | Partir de l'état Photos «jamais demandé» et noter si iOS utilise un picker privé, une permission complète ou limitée. | Le comportement réel iOS est consigné; le picker n'apparaît qu'après consommation. L'absence légitime de prompt avec le picker système n'est pas inventée comme réussite de permission. |
| Refus | Refuser l'accès si iOS présente ce choix, ou rendre Photos indisponible. | Résultat `denied`/`unavailable`; aucun URI ni asset retourné. |
| Annulation | Ouvrir le picker puis choisir Annuler sans sélectionner. | Résultat `cancelled`; aucune photo simulée et aucune réouverture automatique. |
| Succès | Choisir une seule photo de test. | Un asset borné est retourné; URI, dimensions et contenu restent absents des événements génériques et des captures partagées. |
| Arrière-plan/reprise | Mettre l'app en arrière-plan avec le picker ouvert puis revenir. | Le picker reprend ou termine selon iOS, une seule fois; l'app ne lance pas un second picker. |
| Expiration | Laisser expirer le grant avant la présentation. | Aucun picker ne s'ouvre. |
| Perte réseau après effet | Couper le réseau après sélection et avant le résultat. | La sélection n'est pas redemandée; même résultat resoumis en mémoire ou issue incertaine après perte du processus. |
| Aucun rejeu | Dupliquer la notification et reconnecter après sélection/annulation. | Aucun second picker; aucune sélection automatique. |

```text
Résultat photos: NOT RUN
Permission initiale / mode limité:
Issue observée par cas:
request_id expurgés:
Preuves:
Anomalies:
```

## `iphone.mail.compose`

| Cas manuel | Procédure | Résultat attendu et preuve |
|---|---|---|
| Dialogue de permission | Observer l'ouverture avec Mail configuré puis non disponible. MailComposer n'affiche normalement pas de permission de confidentialité. | Consigner `N/A` pour le prompt si confirmé; le composeur visible reste obligatoire et l'indisponibilité est explicite. |
| Refus/indisponibilité | Tester sans compte ou lorsque le composeur est indisponible. | Résultat `denied` avec raison publique `unavailable`, jamais une réussite simulée. |
| Annulation | Ouvrir puis fermer le composeur sans envoyer. | Résultat `cancelled`; aucun mail n'est envoyé ou recomposé automatiquement. |
| Succès | Ouvrir avec destinataire et texte de test, puis effectuer l'action utilisateur choisie. | Le succès valide l'issue rapportée par le composeur, pas la livraison. Destinataires, sujet et corps sont absents des événements génériques. |
| Arrière-plan/reprise | Mettre l'app en arrière-plan avec le composeur ouvert puis revenir. | Un seul composeur; résultat terminal unique après fermeture. |
| Expiration | Attendre l'expiration avant `execute`. | Aucun composeur Mail ne s'ouvre. |
| Perte réseau après effet | Couper le réseau après fermeture/action du composeur, avant le résultat serveur. | Même résultat resoumis sans rouvrir Mail; après perte du processus, aucune reprise automatique. |
| Aucun rejeu | Dupliquer la notification et reconnecter après annulation/action. | Aucun second composeur et aucun envoi silencieux. |

```text
Résultat mail: NOT RUN
Disponibilité initiale de MailComposer:
Issue observée par cas:
request_id expurgés:
Preuves:
Anomalies:
```

## `iphone.sms.compose`

| Cas manuel | Procédure | Résultat attendu et preuve |
|---|---|---|
| Dialogue de permission | Observer l'ouverture sur un iPhone capable d'envoyer des SMS puis lorsque le service est indisponible. Expo SMS n'affiche normalement pas de permission de confidentialité. | Consigner `N/A` pour le prompt si confirmé; seul un composeur visible est accepté. |
| Refus/indisponibilité | Tester avec service SMS indisponible ou restriction iOS appropriée. | Résultat `denied`/`unavailable`; aucune réussite simulée. |
| Annulation | Ouvrir puis fermer le composeur sans envoyer. | Résultat `cancelled`; aucun SMS n'est envoyé ou recomposé automatiquement. |
| Succès | Présenter destinataire et texte de test, puis effectuer l'action utilisateur choisie. | Le résultat décrit le composeur, pas la livraison opérateur. Numéro et corps ne figurent ni au board, ni sur WebSocket, ni dans l'audit générique. |
| Arrière-plan/reprise | Mettre l'app en arrière-plan avec le composeur ouvert puis revenir. | Un seul composeur; aucune nouvelle présentation au resume. |
| Expiration | Attendre l'expiration avant `execute`. | Aucun composeur SMS ne s'ouvre. |
| Perte réseau après effet | Couper le réseau après fermeture/action, avant le résultat serveur. | Même résultat resoumis sans rouvrir le composeur; aucune reprise automatique après perte du processus. |
| Aucun rejeu | Dupliquer la notification et reconnecter après annulation/action. | Aucun second composeur et aucun envoi silencieux. |

```text
Résultat SMS: NOT RUN
Disponibilité initiale de SMS:
Issue observée par cas:
request_id expurgés:
Preuves:
Anomalies:
```

## Contrôles de sortie

La validation peut être déclarée complète seulement si les six fiches portent
une issue explicite pour chaque ligne: `PASS`, `FAIL`, `BLOCKED` ou `N/A` avec
justification. Avant publication de la preuve:

- vérifier que les refus, annulations et expirations ne sont jamais affichés
  comme succès;
- confirmer l'absence de second effet après notification dupliquée, reconnexion
  et reprise d'application;
- confirmer que les payloads WebSocket/board/audit sont expurgés;
- confirmer que l'API autoritative conserve l'issue corrélée attendue;
- consigner séparément build/signature/install/launch, dialogues iOS et résultat
  fonctionnel observé.

Tant que cette fiche n'est pas remplie avec des preuves d'appareil, le statut
reste **MANUAL VALIDATION REQUIRED**.
