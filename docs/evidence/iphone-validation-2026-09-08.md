# Preuve de validation iPhone — 2026-09-08

## Verdict

**MANUAL VALIDATION REQUIRED — BLOCKED, NOT PASSED**

La tentative s'est arrêtée avant l'installation et l'exécution de l'app: le
guest macOS QEMU ne recevait aucun iPhone physique. Aucun dialogue de permission,
picker, composeur ou effet natif n'a été observé. Ce fichier est une preuve de
blocage de transport, pas une preuve de validation fonctionnelle.

## Contexte observé

- Date: 2026-09-08, fuseau America/Montreal
- Environnement: macOS 15.7.7 dans une topologie USB virtualisée/QEMU
- Xcode: 26.3
- Baseline Git au début de la tranche: `e7ca70166462e1133cb621aaad5e0575817fd146`
- État du build v0.11: working tree en cours; aucune installation physique
- Données et identifiants d'appareil: volontairement omis

## Diagnostic exécuté

Commande read-only:

```text
python3 /Users/ales27pm/.codex/skills/connect-xcode-iphone/scripts/diagnose_iphone_connection.py
```

Projection expurgée du résultat:

```text
classification="guest_transport_missing"
ready=false
xcode="Xcode 26.3"
usb_enumerated=false
virtualized_usb_topology=true
device_visible_or_cached=true
paired=true
tunnel_connected=false
ddi_services_available=false
devicectl_available=false
xcdevice_available=false
```

Les identifiants/UDID découverts par les outils Apple ne sont pas reproduits
dans cette preuve. Une entrée jumelée en cache ne prouve ni présence physique,
ni disponibilité CoreDevice.

## Couche bloquante

Le guest n'énumère pas l'iPhone en USB. Le diagnostic de pairing, de tunnel ou
de DDI ne peut donc pas être interprété comme un échec du code monGARS. La
prochaine action nécessaire est un passthrough USB host-vers-guest pris en
charge, ou un chemin privé de développement réseau déjà approuvé et fiable.
La validation doit recommencer depuis `docs/26-iphone-physical-device-validation.md`
après restauration du transport.

## Matrice des capabilities

| Capability | Installation/launch | Permission/UI | Refus | Annulation | Succès | Réseau/kill/non-rejeu | Statut |
|---|---|---|---|---|---|---|---|
| `iphone.location.current` | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | BLOCKED |
| `iphone.contacts.lookup` | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | BLOCKED |
| `iphone.calendar.events` | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | BLOCKED |
| `iphone.photos.pick` | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | BLOCKED |
| `iphone.mail.compose` | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | BLOCKED |
| `iphone.sms.compose` | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | NOT RUN | BLOCKED |

## Ce qui peut et ne peut pas être affirmé

- **QUALIFIED par automatisation:** contrats typés, consommation du grant avant
  exécution, refus du rejeu, perte de réponse conservée comme incertaine et
  exclusion des effets sensibles de l'outbox mobile.
- **NON QUALIFIED physiquement:** tous les dialogues iOS, permissions, chemins
  de refus/annulation/succès, background/resume, pertes réseau et kill après
  effet.
- Aucun `PASS` physique ne doit être déduit de cette session.
