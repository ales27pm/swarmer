# 10 — iPhone Native Capabilities

## Objectif

Définir ce que les agents peuvent demander au iPhone via l'orchestrateur.

## Règle

Les agents ne touchent jamais directement le téléphone. Ils passent par:

```text
Agent → Orchestrator → Phone Capability Broker → iPhone App → iOS Permission/UI → Result
```

## Capability matrix

| Capability | Tool id | Expo/iOS approach | Human confirmation | Notes |
|---|---|---|---|---|
| Appel téléphonique | `phone.call.prepare` | `tel:` deep link / native module | Oui | Prépare l'appel, l'utilisateur déclenche. |
| SMS/iMessage | `message.sms.compose` | MessageUI/native compose | Oui | iOS ne permet pas l'envoi arbitraire silencieux. |
| Email | `email.compose` | `mailto:` / composer / Gmail API connecté | Souvent oui | Draft/compose plus safe que send direct. |
| Calendrier lire | `calendar.events.read` | Expo Calendar / EventKit | Permission iOS | Full access requis pour lire. |
| Calendrier créer | `calendar.event.create` | Expo Calendar / EventKitUI | Permission ou editor UI | Write-only possible selon iOS/version. |
| Rappels | `reminders.read/write` | EventKit native custom | Permission iOS | Peut nécessiter custom module. |
| Contacts | `contacts.search` | Expo Contacts | Permission iOS | Minimiser champs retournés. |
| Position actuelle | `location.current` | Expo Location | Permission iOS + app approval | Foreground d'abord. |
| Photos picker | `photos.pick` | Expo ImagePicker | UI utilisateur | Préférer picker à full library. |
| Photos library | `photos.library.read` | MediaLibrary/Photos | Permission iOS | Sensitive, ask by default. |
| Caméra | `camera.capture` | Expo Camera | Permission iOS | UI visible. |
| Micro/audio | `audio.record` | expo-audio | Permission iOS | Doit être visible/consenti. |
| Notifications | `notification.schedule` | Expo Notifications | Permission iOS | Local notifications utiles. |
| Secure storage | `securestore.get/set` | Expo SecureStore | Rule-based | Ne jamais retourner secret au LLM brut. |
| Clipboard | `clipboard.read/write` | Expo Clipboard | Ask | Sensitive selon contenu. |
| Files app | `document.pick` | DocumentPicker | UI utilisateur | File grants par fichier. |

## Request format

```json
{
  "type": "iphone_capability_request",
  "request_id": "iphreq_...",
  "agent_id": "calendar-worker",
  "task_id": "tsk_...",
  "capability": "calendar.events.read",
  "reason": "Trouver les disponibilités pour planifier un rendez-vous",
  "scope": {
    "from": "2026-09-04T00:00:00-04:00",
    "to": "2026-09-11T23:59:59-04:00",
    "fields": ["title", "start", "end", "location"]
  },
  "sensitivity": "personal",
  "approval_required": true
}
```

## Result format

```json
{
  "type": "iphone_capability_result",
  "request_id": "iphreq_...",
  "status": "ok",
  "data": [],
  "redactions": ["attendee_email"],
  "expires_at": "2026-09-04T12:00:00Z"
}
```

## Data minimization

Toujours retourner le minimum:

- Contacts: nom + téléphone/email demandé, pas toute la fiche.
- Photos: asset id + metadata ou image choisie, pas toute la galerie.
- Location: précision adaptée, pas tracking continu par défaut.
- Calendar: fenêtre temporelle limitée.

## Permission tiers

### Tier 0 — No permission

- device status non sensible;
- app version;
- connectivity.

### Tier 1 — App permission rule

- lire cache non sensible;
- schedule local notification déjà autorisée.

### Tier 2 — iOS permission

- contacts;
- calendar;
- location;
- camera;
- microphone;
- photos.

### Tier 3 — User approval per request

- partager position à un agent;
- lire contacts;
- envoyer/drafter communication;
- lire photos;
- modifier calendrier.

## iOS hard limits

- Le système ne doit pas promettre “accès total brut”. iOS force des permissions et parfois des interfaces de confirmation.
- SMS/iMessage ne doit pas être conçu comme envoi silencieux automatique.
- Certaines APIs exigent un module natif custom et donc un development build Expo.
- Certaines données système ne sont pas accessibles aux apps tierces.

## MVP capabilities

Phase 1:

- `location.current`
- `contacts.search`
- `calendar.events.read`
- `calendar.event.create`
- `photos.pick`
- `camera.capture`
- `notification.schedule`
- `securestore.get/set`

Phase 2:

- `message.sms.compose`
- `email.compose`
- `phone.call.prepare`
- custom EventKit reminders module
- on-device LLM module
