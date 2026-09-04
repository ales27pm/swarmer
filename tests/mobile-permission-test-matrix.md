# Mobile Permission Test Matrix

| Capability | No permission | Denied | Approved | Offline | Expected |
|---|---|---|---|---|---|
| location.current | prompt | error state | minimal result | queued/fail | no crash |
| contacts.search | prompt | empty denied | scoped fields | fail | redacted result |
| calendar.events.read | prompt | denied | date-window result | fail | no full dump |
| calendar.event.create | prompt/editor | denied | event created/draft | queued? | confirmation shown |
| photos.pick | picker | cancelled | selected asset | fail | no full library |
| camera.capture | prompt | denied | captured asset | fail | UI visible |
| audio.record | prompt | denied | audio file | fail | recording indicator |
| notification.schedule | prompt | denied | scheduled | queued | status visible |
| sms.compose | composer | cancelled | user sends | n/a | no silent send |
| phone.call.prepare | call UI | cancelled | user calls | n/a | no silent call |
