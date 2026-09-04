Feature: monGARS Swarm App MVP

  Scenario: Pair iPhone with Ubuntu
    Given the Ubuntu control plane is running
    When I enter a valid pairing code in the iPhone app
    Then the app stores a device token securely
    And bootstrap sync succeeds

  Scenario: Create a simple task
    Given the iPhone is paired
    When I send "résume l'état du système"
    Then a task is created on Ubuntu
    And the task appears in the iPhone task list
    And the task eventually completes

  Scenario: Ask permission before file write
    Given a code worker proposes a patch
    When the gateway detects a file write
    Then an approval card is shown on iPhone
    And no write happens before approval

  Scenario: Allow once executes only scoped action
    Given an approval for one file write is pending
    When I tap Allow once
    Then only that file write is executed
    And an audit event is recorded

  Scenario: Deny blocks execution
    Given an approval is pending
    When I tap Deny
    Then no executor runs the action
    And the task is marked blocked or replanned

  Scenario: Agent requests iPhone location
    Given an agent needs current location
    When the request reaches the iPhone
    Then the app shows a permission request
    And only minimal location data is returned after approval

  Scenario: Offline message queues locally
    Given the iPhone is offline
    When I send a message
    Then it is saved to sync_outbox
    When network returns
    Then the message syncs to Ubuntu
