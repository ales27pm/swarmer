# 21 — Acceptance Criteria

## MVP acceptance

### AC-001 — Pairing

Given Ubuntu API is running  
When the iPhone app enters a valid pairing code  
Then the device token is saved securely  
And bootstrap sync succeeds.

### AC-002 — Chat task

Given the device is paired  
When the user sends a command  
Then a task is created on Ubuntu  
And the app receives task status updates live.

### AC-003 — State source of truth

Given a task status changes on Ubuntu  
When the iPhone reconnects  
Then the iPhone replica updates to Ubuntu's status.

### AC-004 — Offline outbox

Given the iPhone is offline  
When the user sends a message  
Then the message is stored in outbox  
And is pushed when connection returns.

### AC-005 — Orchestrator JSON

Given an input task  
When the orchestrator responds  
Then response validates against schema  
Or is retried/blocked without execution.

### AC-006 — Permission request

Given an agent proposes a file write  
When Gateway evaluates it  
Then an approval is created  
And the iPhone shows the approval card.

### AC-007 — Approval allow once

Given an approval is pending  
When the user allows once  
Then exactly that action executes  
And audit log records the decision.

### AC-008 — Approval deny

Given an approval is pending  
When the user denies  
Then no executor action happens  
And the task status becomes blocked or replanned.

### AC-009 — Memory search

Given memory contains project facts  
When a related task starts  
Then Memory Service returns relevant context  
And the task log records memory ids used.

### AC-010 — iPhone capability request

Given an agent requests current location  
When the user approves on iPhone  
Then the iPhone returns a minimal location result  
And the result TTL is enforced.

### AC-011 — Feedback event

Given a task completes  
When the user rates it  
Then a feedback event is stored  
And can be exported to eval JSONL.

### AC-012 — Local checks

Given a release candidate  
When local check script runs  
Then mobile/backend/schema tests pass.

## Done means

- No direct model execution bypass.
- No DB writes outside State Service.
- No iPhone data access outside Capability Broker.
- Every sensitive action has approval/audit.
- Every core flow has at least one automated test.
