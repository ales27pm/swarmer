# Backend Test Matrix

| Area | Test | Expected |
|---|---|---|
| State | create_task | task persisted |
| State | update_task_status | valid transition only |
| Sync | bootstrap | snapshot + cursor |
| Sync | push duplicate op | idempotent |
| Gateway | file.read allowed root | allow |
| Gateway | .env read | deny |
| Gateway | file.write project | ask |
| Gateway | dangerous shell | deny |
| Parser | valid tool JSON | accepted |
| Parser | invalid JSON | retry/block |
| Parser | model refusal | permission_request normalized |
| Registry | heartbeat timeout | agent offline |
| Memory | remember/search | relevant result |
| Audit | hash chain | valid chain |
| Feedback | task completed event | stored/exportable |
