import Foundation

@MainActor
private final class FakeBackgroundTask: BackgroundGenerationTask {
  var expiration: (@Sendable () -> Void)?
  var progress: [Int] = []
  var completions: [Bool] = []
  func setExpirationHandler(_ handler: @escaping @Sendable () -> Void) { expiration = handler }
  func reportOutputBytes(_ count: Int) { progress.append(count) }
  func complete(success: Bool, outputBytes: Int) { completions.append(success) }
}

@MainActor
private final class FakeScheduler: BackgroundGenerationScheduling {
  var isForeground = true
  var isBackground = false
  var osSupported = true
  var gpuSupported = true
  var registrationAllowed = true
  var rejection: BackgroundGenerationSubmissionError?
  var launchImmediately = true
  var submissions: [String] = []
  var requestedDevices: [BackgroundGenerationDevice] = []
  var cancelled: [String] = []
  var handlers: [String: @MainActor (any BackgroundGenerationTask) -> Void] = [:]
  var tasks: [String: FakeBackgroundTask] = [:]
  func support() -> BackgroundGenerationSupport {
    BackgroundGenerationSupport(osSupported: osSupported, gpuSupported: gpuSupported)
  }
  func register(identifier: String, launch: @escaping @MainActor (any BackgroundGenerationTask) -> Void) -> Bool {
    precondition(handlers[identifier] == nil, "duplicate registration")
    if registrationAllowed { handlers[identifier] = launch }
    return registrationAllowed
  }
  func submit(identifier: String, device: BackgroundGenerationDevice) throws {
    submissions.append(identifier)
    requestedDevices.append(device)
    if let rejection { throw rejection }
    if launchImmediately { admit(identifier) }
  }
  func admit(_ identifier: String) {
    let task = FakeBackgroundTask()
    tasks[identifier] = task
    handlers[identifier]?(task)
  }
  func cancel(identifier: String) { cancelled.append(identifier) }
}

private actor CancellationBarrier {
  var entered = false
  private var waiter: CheckedContinuation<Void, Never>?
  func cancel() async {
    entered = true
    await withCheckedContinuation { waiter = $0 }
  }
  func release() { waiter?.resume(); waiter = nil }
}

private struct Failure: Error { let message: String }
@MainActor
private func check(_ condition: @autoclosure () -> Bool, _ message: String) throws {
  if !condition() { throw Failure(message: message) }
}
@MainActor
private func settle() async { for _ in 0..<20 { await Task.yield() } }

@main
private struct BackgroundGenerationTests {
  @MainActor static func main() async throws {
    try await fallbackDoesNotSubmit()
    try await refusalsDoNotGrantPermission()
    try await cpuAdmissionDoesNotClaimGPUPermission()
    try await cpuRefusalStaysForegroundOnly()
    try await admissionProgressAndCompletion()
    try await expirationWaitsForNativeBarrier()
    try await backgroundRejectsPendingAdmission()
    try await resigningActiveInvalidatesBeforeUIKitStateChanges()
    try await cancelledAdmissionRejectsLateTask()
    try await timeoutRejectsLateTask()
    try await staleCallbacksCannotAffectNewGeneration()
    try lifecycleEntryReconcilesCurrentState()
    try staleActiveCallbackCannotReopenBackground()
    try staleResumePreservesFullBackgroundCancellation()
    print("14 background generation lifecycle tests passed")
  }

  @MainActor static func lifecycleEntryReconcilesCurrentState() throws {
    var fence = GenerationActivityFence()
    let background = fence.begin()
    try check(fence.reconcile(inactive: true, epoch: background), "initial suspension")
    let resume = fence.begin()
    // An OLD suspend Task enters after the newer foreground event. Its OS read
    // is active; it must reconcile false, not blindly set true or just return.
    let oldSuspendEnteringLate = fence.begin()
    try check(fence.reconcile(inactive: false, epoch: oldSuspendEnteringLate), "latest entrant reads current foreground")
    try check(!fence.reconcile(inactive: false, epoch: resume), "older await fenced")
    try check(!fence.isSuspended, "late old callback cannot leave app suspended forever")
    let staleActive = fence.begin()
    let nextBackground = fence.begin()
    try check(fence.reconcile(inactive: true, epoch: nextBackground), "new background wins")
    try check(!fence.reconcile(inactive: false, epoch: staleActive), "old foreground read cannot unsuspend newer background")
    try check(fence.isSuspended, "must remain suspended in background")
  }

  @MainActor static func staleActiveCallbackCannotReopenBackground() throws {
    let scheduler = FakeScheduler()
    let controller = BackgroundGenerationController(scheduler: scheduler)
    controller.willResignActive()
    scheduler.isForeground = false
    controller.didBecomeActive() // A delayed independently scheduled callback.
    try check(controller.isInactive(), "OS background state rejects old active callback")
    try check(!controller.mayRun(operationId: UUID()), "cannot start background GPU without lease")
    scheduler.isForeground = true
    try check(controller.isInactive(), "early OS active value alone does not clear the notification fence")
    controller.didBecomeActive()
    try check(!controller.isInactive(), "actual active callback reopens foreground execution")
  }

  @MainActor static func staleResumePreservesFullBackgroundCancellation() throws {
    let scheduler = FakeScheduler()
    let controller = BackgroundGenerationController(scheduler: scheduler)
    var fence = GenerationActivityFence()
    let suspend = fence.begin()
    scheduler.isForeground = false
    scheduler.isBackground = true
    controller.willResignActive()
    let oldResumeEnteringLate = fence.begin()
    let snapshot = controller.activitySnapshot()
    try check(fence.reconcile(inactive: snapshot.inactive, epoch: oldResumeEnteringLate), "late callback reconciles live background")
    try check(!fence.reconcile(inactive: true, epoch: suspend), "old pending callback is fenced")
    try check(snapshot.shouldCancelAll(requested: false), "late resume must preserve CoreML/GGUF/load/import cancellation")
    try check(fence.isSuspended, "all new operations remain blocked")
    scheduler.isBackground = false // Merely inactive, without a background event.
    let inactive = controller.activitySnapshot()
    try check(!inactive.shouldCancelAll(requested: false), "early inactivity retains narrower MLX cancellation")
    try check(inactive.shouldCancelAll(requested: true), "explicit suspend still cancels all")
  }

  @MainActor static func fallbackDoesNotSubmit() async throws {
    for variant in 0..<2 {
      let scheduler = FakeScheduler()
      if variant == 0 { scheduler.osSupported = false; scheduler.gpuSupported = false }
      if variant == 1 { scheduler.isForeground = false }
      let controller = BackgroundGenerationController(scheduler: scheduler)
      let id = UUID()
      await controller.prepare(operationId: id, cancel: {})
      let status = controller.status()
      try check(scheduler.submissions.isEmpty, "fallback must not submit")
      try check(status.entitlementGranted == nil, "OS/resource/foreground checks do not establish entitlement")
      try check(!controller.mayContinue(operationId: id), "fallback must cancel on background")
      try check(status.reason == ["os_unsupported", "foreground_required"][variant], "precise fallback reason")
    }
  }

  @MainActor static func refusalsDoNotGrantPermission() async throws {
    for variant in 0..<3 {
      let scheduler = FakeScheduler()
      if variant == 0 { scheduler.rejection = .notPermitted }
      if variant == 1 { scheduler.rejection = .busy }
      if variant == 2 { scheduler.registrationAllowed = false }
      let controller = BackgroundGenerationController(scheduler: scheduler)
      let id = UUID()
      await controller.prepare(operationId: id, cancel: {})
      let status = controller.status()
      try check(status.supported, "GPU availability must be independent from permission")
      try check(status.entitlementGranted == (variant == 0 ? false : nil), "only explicit permission rejection establishes false")
      try check(status.reason == ["not_permitted", "system_busy", "registration_failed"][variant], "refusal reason")
      try check(controller.mayRun(operationId: id), "refusal preserves foreground generation")
      try check(!controller.mayContinue(operationId: id), "refusal never allows background GPU")
    }
  }

  @MainActor static func cpuAdmissionDoesNotClaimGPUPermission() async throws {
    let scheduler = FakeScheduler()
    scheduler.gpuSupported = false
    let controller = BackgroundGenerationController(scheduler: scheduler)
    let id = UUID()
    try check(controller.status().supported, "CPU continued processing only requires supported OS")
    try check(controller.preferredExecutionDevice() == .cpu, "load and generation must agree on CPU")
    let selected = await controller.prepare(operationId: id, cancel: {})
    try check(selected == .cpu && scheduler.requestedDevices == [.cpu], "request CPU without a GPU requirement")
    let status = controller.status()
    try check(status.active && !status.gpuSupported && status.executionDevice == "cpu", "CPU admission is independently usable")
    try check(status.entitlementGranted == nil, "CPU admission proves no GPU entitlement")
    try check(status.reason == "cpu_fallback", "fallback is explicit")
    scheduler.isForeground = false
    scheduler.isBackground = true
    controller.willResignActive()
    try check(controller.mayContinue(operationId: id), "admitted CPU generation survives background")
    controller.reportOutput(operationId: id, bytes: 7)
    controller.finish(operationId: id, success: true)
    try check(controller.status().executionDevice == "cpu" && controller.status().outputBytes == 7, "retain honest CPU outcome")
  }

  @MainActor static func cpuRefusalStaysForegroundOnly() async throws {
    let scheduler = FakeScheduler()
    scheduler.gpuSupported = false
    scheduler.rejection = .notPermitted
    let controller = BackgroundGenerationController(scheduler: scheduler)
    let id = UUID()
    let selected = await controller.prepare(operationId: id, cancel: {})
    try check(selected == .cpu, "refused continuation still uses CPU in foreground")
    try check(!controller.status().active && controller.status().entitlementGranted == nil, "CPU refusal cannot claim GPU entitlement denied")
    scheduler.isForeground = false
    try check(!controller.mayRun(operationId: id), "refused CPU task cannot continue in background")
  }

  @MainActor static func admissionProgressAndCompletion() async throws {
    let scheduler = FakeScheduler()
    let controller = BackgroundGenerationController(scheduler: scheduler)
    let id = UUID()
    try check(controller.status().entitlementGranted == nil, "permission initially unknown")
    await controller.prepare(operationId: id, cancel: {})
    let task = scheduler.tasks[scheduler.submissions[0]]!
    try check(controller.status().active && controller.status().entitlementGranted == true, "only admission proves permission")
    try check(scheduler.requestedDevices == [.gpu] && controller.status().executionDevice == "gpu", "GPU path retains explicit GPU request")
    scheduler.isForeground = false
    try check(controller.mayRun(operationId: id), "admitted generation can continue")
    try check(!controller.mayRun(operationId: UUID()), "lease cannot cover another operation")
    controller.reportOutput(operationId: id, bytes: "é🐬".utf8.count)
    controller.reportOutput(operationId: id, bytes: 1)
    controller.reportOutput(operationId: UUID(), bytes: 100)
    try check(task.progress == [0, 6], "real monotonic bytes, not tokens or stale progress")
    try check(task.completions.isEmpty, "progress alone cannot finish the task")
    controller.finish(operationId: id, success: true)
    controller.finish(operationId: id, success: true)
    try check(task.completions == [true], "completion exactly once after producer returns")
    try check(!controller.status().active && controller.status().outputBytes == 6, "last output metadata survives")
  }

  @MainActor static func expirationWaitsForNativeBarrier() async throws {
    let scheduler = FakeScheduler()
    let controller = BackgroundGenerationController(scheduler: scheduler)
    let barrier = CancellationBarrier()
    let id = UUID()
    await controller.prepare(operationId: id, cancel: { await barrier.cancel() })
    let task = scheduler.tasks[scheduler.submissions[0]]!
    task.expiration?()
    while !(await barrier.entered) { await Task.yield() }
    try check(controller.status().state == "expiring", "expiry state while producer drains")
    try check(!controller.mayContinue(operationId: id), "expired lease grants no new GPU work")
    try check(task.completions.isEmpty, "must await real cancellation barrier")
    // The producer's final native barrier can return before the cancel callback
    // resumes. Even a .stop result must be normalized to cancelled by its caller.
    let leaseCancelled = controller.finish(operationId: id, success: true)
    try check(leaseCancelled, "expiration must fence a late native stop result")
    await barrier.release()
    await settle()
    try check(task.completions == [false], "expiry wins completion race, exactly once")
    try check(controller.status().state == "cancelled", "expiry preserves cancelled outcome")
  }

  @MainActor static func backgroundRejectsPendingAdmission() async throws {
    let scheduler = FakeScheduler()
    scheduler.launchImmediately = false
    let controller = BackgroundGenerationController(scheduler: scheduler)
    let id = UUID()
    let pending = Task { await controller.prepare(operationId: id, cancel: {}) }
    while scheduler.submissions.isEmpty { await Task.yield() }
    scheduler.isForeground = false
    let identifier = scheduler.submissions[0]
    scheduler.admit(identifier)
    _ = await pending.value
    try check(scheduler.tasks[identifier]?.completions == [false], "late admission cannot start after background")
    try check(!controller.mayRun(operationId: id), "no permission at dispatch after background")
    try check(controller.status().entitlementGranted == nil, "rejected stale admission proves no usable permission")
  }

  @MainActor static func resigningActiveInvalidatesBeforeUIKitStateChanges() async throws {
    let scheduler = FakeScheduler()
    scheduler.launchImmediately = false
    let controller = BackgroundGenerationController(scheduler: scheduler)
    let id = UUID()
    let pending = Task { await controller.prepare(operationId: id, cancel: {}) }
    while scheduler.submissions.isEmpty { await Task.yield() }
    // UIKit may still report .active inside willResignActiveNotification.
    controller.willResignActive()
    _ = await pending.value
    try check(scheduler.isForeground && !controller.mayRun(operationId: id), "early lifecycle fence must reject GPU dispatch")
    scheduler.admit(scheduler.submissions[0])
    try check(!controller.status().active, "late admission after inactivity stays rejected")
    controller.didBecomeActive()
    try check(controller.mayRun(operationId: id), "foreground fallback resumes only when active again")
  }

  @MainActor static func cancelledAdmissionRejectsLateTask() async throws {
    let scheduler = FakeScheduler()
    scheduler.launchImmediately = false
    let controller = BackgroundGenerationController(scheduler: scheduler)
    let id = UUID()
    let pending = Task { await controller.prepare(operationId: id, cancel: {}) }
    while scheduler.submissions.isEmpty { await Task.yield() }
    pending.cancel()
    _ = await pending.value
    let identifier = scheduler.submissions[0]
    scheduler.admit(identifier)
    try check(scheduler.tasks[identifier]?.completions == [false], "cancelled request cannot resurrect")
    try check(!controller.status().active, "cancelled admission stays inactive")
  }

  @MainActor static func timeoutRejectsLateTask() async throws {
    let scheduler = FakeScheduler()
    scheduler.launchImmediately = false
    let controller = BackgroundGenerationController(scheduler: scheduler, admissionTimeout: .milliseconds(2))
    await controller.prepare(operationId: UUID(), cancel: {})
    try check(controller.status().reason == "admission_timeout", "admission has a finite wait")
    let identifier = scheduler.submissions[0]
    scheduler.admit(identifier)
    try check(scheduler.tasks[identifier]?.completions == [false], "timed-out task cannot claim completed foreground work")
  }

  @MainActor static func staleCallbacksCannotAffectNewGeneration() async throws {
    let scheduler = FakeScheduler()
    let controller = BackgroundGenerationController(scheduler: scheduler)
    let old = UUID()
    await controller.prepare(operationId: old, cancel: {})
    let oldTask = scheduler.tasks[scheduler.submissions[0]]!
    let staleExpiry = oldTask.expiration
    controller.finish(operationId: old, success: true)
    let next = UUID()
    await controller.prepare(operationId: next, cancel: {})
    staleExpiry?()
    controller.finish(operationId: old, success: false)
    controller.reportOutput(operationId: old, bytes: 99)
    await settle()
    try check(controller.mayContinue(operationId: next), "old expiry cannot cancel newer generation")
    try check(controller.status().outputBytes == 0, "old progress cannot pollute next generation")
    try check(Set(scheduler.submissions).count == 2, "concrete IDs registered once per operation")
    controller.finish(operationId: next, success: true)
  }
}
