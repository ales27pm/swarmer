import Foundation
#if os(iOS)
import BackgroundTasks
import UIKit
#endif

struct BackgroundExecutionSnapshot: Sendable {
  let supported: Bool
  let reason: String?
  let osSupported: Bool
  let gpuSupported: Bool
  let entitlementGranted: Bool?
  let active: Bool
  let operationId: String?
  let outputBytes: Int
  let state: String
  let executionDevice: String?
}

enum BackgroundGenerationDevice: String, Sendable {
  case cpu, gpu
}

struct BackgroundGenerationActivity: Sendable {
  let inactive: Bool
  let background: Bool
  func shouldCancelAll(requested: Bool) -> Bool { requested || background }
}

/// Actor-local fence for lifecycle callbacks whose Tasks may enter out of order.
/// Every winning callback reconciles the current OS state, not its event name.
struct GenerationActivityFence: Sendable {
  private var epoch = 0
  private(set) var isSuspended = false
  mutating func begin() -> Int { epoch += 1; return epoch }
  mutating func reconcile(inactive: Bool, epoch: Int) -> Bool {
    guard self.epoch == epoch else { return false }
    isSuspended = inactive
    return true
  }
  func isCurrent(_ epoch: Int) -> Bool { self.epoch == epoch }
  mutating func shutdown() { epoch += 1; isSuspended = true }
}

@MainActor
protocol BackgroundGenerationTask: AnyObject {
  func setExpirationHandler(_ handler: @escaping @Sendable () -> Void)
  func reportProgress(completed: Int, total: Int, outputBytes: Int)
  func complete(success: Bool, outputBytes: Int)
}

struct BackgroundGenerationSupport: Sendable {
  let osSupported: Bool
  let gpuSupported: Bool
}

enum BackgroundGenerationSubmissionError: Error {
  case notPermitted, busy, unavailable
}

@MainActor
protocol BackgroundGenerationScheduling: AnyObject {
  var isForeground: Bool { get }
  var isBackground: Bool { get }
  func support() -> BackgroundGenerationSupport
  func register(identifier: String, launch: @escaping @MainActor (any BackgroundGenerationTask) -> Void) -> Bool
  func submit(identifier: String, device: BackgroundGenerationDevice) throws
  func cancel(identifier: String)
}

/// Owns one finite generation, never a background listener or a queued retry.
/// The scheduler enforces the signed GPU entitlement on admission; iOS has no
/// public SecTask entitlement-inspection API, so its value starts as unknown.
@MainActor
final class BackgroundGenerationController {
  nonisolated static let statusDidChange = Notification.Name("SwarmerBackgroundExecutionDidChange")
  #if os(iOS)
  static let shared = BackgroundGenerationController(scheduler: SystemBackgroundGenerationScheduler())
  #endif

  private struct Operation {
    let id: UUID
    let identifier: String
    let device: BackgroundGenerationDevice
    let maxTokens: Int
    var prepared = false
    var tokenSteps = 0
    var completedWork: Int { (prepared ? 1 : 0) + tokenSteps }
    let cancel: @Sendable () async -> Void
    var task: (any BackgroundGenerationTask)?
    var admission: CheckedContinuation<Void, Never>?
  }

  private let scheduler: any BackgroundGenerationScheduling
  private let admissionTimeout: Duration
  private var operation: Operation?
  private var entitlementGranted: Bool?
  private var executionDevice: BackgroundGenerationDevice?
  private var state = "idle"
  private var reason: String?
  private var outputBytes = 0
  private var foregroundBlocked = false

  init(scheduler: any BackgroundGenerationScheduling, admissionTimeout: Duration = .seconds(3)) {
    self.scheduler = scheduler
    self.admissionTimeout = admissionTimeout
  }

  func status() -> BackgroundExecutionSnapshot {
    let support = scheduler.support()
    let capabilityReason: String? = !support.osSupported ? "os_unsupported"
      : !support.gpuSupported ? "cpu_fallback"
      : entitlementGranted == nil ? "permission_unverified" : nil
    return BackgroundExecutionSnapshot(
      supported: support.osSupported,
      reason: reason ?? capabilityReason,
      osSupported: support.osSupported, gpuSupported: support.gpuSupported,
      entitlementGranted: entitlementGranted, active: operation?.task != nil && state == "active",
      operationId: operation?.id.uuidString, outputBytes: outputBytes, state: state,
      executionDevice: executionDevice?.rawValue
    )
  }

  func preferredExecutionDevice() -> BackgroundGenerationDevice {
    let support = scheduler.support()
    return support.osSupported && !support.gpuSupported ? .cpu : .gpu
  }

  @discardableResult
  func prepare(operationId: UUID, maxTokens: Int, cancel: @escaping @Sendable () async -> Void) async -> BackgroundGenerationDevice {
    let device = preferredExecutionDevice()
    guard operation == nil else { return device }
    executionDevice = device
    outputBytes = 0
    guard maxTokens > 0, maxTokens <= Int.max - 2 else {
      state = "foreground_only"
      reason = "invalid_token_limit"
      notifyStatusChanged()
      return device
    }
    let support = scheduler.support()
    guard support.osSupported, scheduler.isForeground, !foregroundBlocked else {
      state = "foreground_only"
      reason = !support.osSupported ? "os_unsupported" : "foreground_required"
      notifyStatusChanged()
      return device
    }
    let identifier = "org.27pm.mongars.mlx-generation.\(operationId.uuidString)"
    operation = Operation(id: operationId, identifier: identifier, device: device, maxTokens: maxTokens, cancel: cancel)
    state = "requesting"
    reason = device == .cpu ? "cpu_fallback" : nil
    notifyStatusChanged()
    // Register each concrete ID once. Only Info.plist uses the wildcard family.
    // The scheduler retains handlers: retain neither a model nor this controller.
    guard scheduler.register(identifier: identifier, launch: { [weak self] task in
      guard let self else { task.complete(success: false, outputBytes: 0); return }
      self.admit(task, operationId: operationId)
    }) else {
      abandonAdmission(operationId: operationId, reason: "registration_failed")
      return device
    }
    await withTaskCancellationHandler {
      await withCheckedContinuation { continuation in
        guard operation?.id == operationId, !Task.isCancelled else {
          abandonAdmission(operationId: operationId, reason: "request_cancelled")
          continuation.resume()
          return
        }
        operation?.admission = continuation
        do {
          try scheduler.submit(identifier: identifier, device: device)
          Task { [weak self, admissionTimeout] in
            try? await Task.sleep(for: admissionTimeout)
            self?.abandonAdmission(operationId: operationId, reason: "admission_timeout")
          }
        } catch {
          let rejection: String
          switch error {
          case BackgroundGenerationSubmissionError.notPermitted:
            if device == .gpu { entitlementGranted = false }
            rejection = "not_permitted"
          case BackgroundGenerationSubmissionError.busy: rejection = "system_busy"
          default: rejection = "request_failed"
          }
          abandonAdmission(operationId: operationId, reason: rejection)
        }
      }
    } onCancel: {
      Task { @MainActor [weak self] in
        self?.abandonAdmission(operationId: operationId, reason: "request_cancelled")
      }
    }
    return device
  }

  private func admit(_ task: any BackgroundGenerationTask, operationId: UUID) {
    guard operation?.id == operationId, state == "requesting", scheduler.isForeground, !foregroundBlocked else {
      task.complete(success: false, outputBytes: 0)
      abandonAdmission(operationId: operationId, reason: "foreground_required")
      return
    }
    if operation?.device == .gpu { entitlementGranted = true }
    operation?.task = task
    state = "active"
    reason = operation?.device == .cpu ? "cpu_fallback" : nil
    task.setExpirationHandler { [weak self] in
      Task { @MainActor in await self?.expire(operationId: operationId) }
    }
    reportProgress()
    let continuation = operation?.admission
    operation?.admission = nil
    continuation?.resume()
    notifyStatusChanged()
  }

  private func abandonAdmission(operationId: UUID, reason: String) {
    guard let pending = operation, pending.id == operationId, pending.task == nil else { return }
    operation = nil
    scheduler.cancel(identifier: pending.identifier)
    state = "foreground_only"
    self.reason = reason
    pending.admission?.resume()
    notifyStatusChanged()
  }

  func willResignActive() {
    foregroundBlocked = true
    if let pending = operation, pending.task == nil {
      abandonAdmission(operationId: pending.id, reason: "foreground_required")
    }
  }

  func didBecomeActive() {
    // A delayed active callback must never reopen GPU dispatch in background.
    guard scheduler.isForeground else { return }
    foregroundBlocked = false
  }

  func activitySnapshot() -> BackgroundGenerationActivity {
    // Both values are read in one main-actor turn, without a suspension point.
    BackgroundGenerationActivity(
      inactive: foregroundBlocked || !scheduler.isForeground,
      background: scheduler.isBackground
    )
  }

  func isInactive() -> Bool { activitySnapshot().inactive }

  func mayRun(operationId: UUID) -> Bool {
    (scheduler.isForeground && !foregroundBlocked) || mayContinue(operationId: operationId)
  }

  func mayContinue(operationId: UUID) -> Bool {
    operation?.id == operationId && operation?.task != nil && state == "active"
  }

  func reportOutput(operationId: UUID, bytes: Int) {
    guard operation?.id == operationId, state == "active", bytes >= outputBytes else { return }
    outputBytes = bytes
    reportProgress()
  }

  func reportPreparation(operationId: UUID) {
    reportWork(operationId: operationId, completedUnits: 1)
  }

  func reportTokenStep(operationId: UUID, count: Int) {
    guard let current = operation, count > 0, count <= current.maxTokens else { return }
    reportWork(operationId: operationId, completedUnits: count + 1)
  }

  func reportWork(operationId: UUID, completedUnits: Int) {
    guard let current = operation, current.id == operationId, state == "active",
          completedUnits > current.completedWork, completedUnits <= current.maxTokens + 1 else { return }
    // A finished token step implies preparation already finished. Its callback
    // may arrive first; accept that evidence and ignore later lower counters.
    operation?.prepared = true
    operation?.tokenSteps = completedUnits - 1
    reportProgress()
  }

  private func reportProgress() {
    guard let current = operation, state == "active" else { return }
    // Preparation + bounded generation + a final native producer/stream barrier.
    // Bytes are independent output evidence, never an invented token estimate.
    current.task?.reportProgress(
      completed: current.completedWork, total: current.maxTokens + 2, outputBytes: outputBytes
    )
  }

  private func expire(operationId: UUID) async {
    guard let current = operation, current.id == operationId, state == "active" else { return }
    state = "expiring"
    reason = "user_or_system_cancelled"
    notifyStatusChanged()
    // Await the actual native producer and its GPU synchronization barrier.
    await current.cancel()
    finish(operationId: operationId, success: false, cancelled: true)
  }

  @discardableResult
  func finish(operationId: UUID, success: Bool, cancelled: Bool = false) -> Bool {
    guard let current = operation, current.id == operationId else { return cancelled }
    let didExpire = state == "expiring"
    operation = nil
    current.admission?.resume()
    if let task = current.task {
      let completedSuccessfully = success && !didExpire && !cancelled
      if completedSuccessfully {
        // The caller has joined the producer and drained its execution stream.
        // EOS can finish before the limit: complete only the work actually done,
        // including one final barrier unit, without claiming unused token steps.
        let finishedWork = current.completedWork + 1
        task.reportProgress(completed: finishedWork, total: finishedWork, outputBytes: outputBytes)
      }
      task.complete(success: completedSuccessfully, outputBytes: outputBytes)
    } else {
      scheduler.cancel(identifier: current.identifier)
    }
    state = didExpire || cancelled ? "cancelled" : success ? "completed" : "failed"
    if !didExpire { reason = current.device == .cpu ? "cpu_fallback" : nil }
    notifyStatusChanged()
    return didExpire || cancelled
  }

  private func notifyStatusChanged() {
    NotificationCenter.default.post(name: Self.statusDidChange, object: nil)
  }
}

#if os(iOS)
@available(iOS 26.0, *)
@MainActor
private final class SystemBackgroundGenerationTask: BackgroundGenerationTask {
  private let task: BGContinuedProcessingTask
  init(_ task: BGContinuedProcessingTask) { self.task = task }
  func setExpirationHandler(_ handler: @escaping @Sendable () -> Void) { task.expirationHandler = handler }
  func reportProgress(completed: Int, total: Int, outputBytes: Int) {
    task.progress.totalUnitCount = Int64(total)
    task.progress.completedUnitCount = Int64(completed)
    #if DEBUG
    NSLog("BackgroundGenerationProgress completed=%lld total=%lld outputBytes=%lld fraction=%f",
          Int64(completed), Int64(total), Int64(outputBytes), task.progress.fractionCompleted)
    #endif
    task.updateTitle("Génération MLX", subtitle: "\(outputBytes) octets de texte produits")
  }
  func complete(success: Bool, outputBytes: Int) {
    task.expirationHandler = nil
    task.setTaskCompleted(success: success)
  }
}

@MainActor
private final class SystemBackgroundGenerationScheduler: BackgroundGenerationScheduling {
  var isForeground: Bool { UIApplication.shared.applicationState == .active }
  var isBackground: Bool { UIApplication.shared.applicationState == .background }
  func support() -> BackgroundGenerationSupport {
    if #available(iOS 26.0, *) {
      return BackgroundGenerationSupport(
        osSupported: true, gpuSupported: BGTaskScheduler.supportedResources.contains(.gpu)
      )
    }
    return BackgroundGenerationSupport(osSupported: false, gpuSupported: false)
  }
  func register(identifier: String, launch: @escaping @MainActor (any BackgroundGenerationTask) -> Void) -> Bool {
    guard #available(iOS 26.0, *) else { return false }
    return BGTaskScheduler.shared.register(forTaskWithIdentifier: identifier, using: .main) { task in
      MainActor.assumeIsolated {
        guard let continued = task as? BGContinuedProcessingTask else {
          task.setTaskCompleted(success: false)
          return
        }
        launch(SystemBackgroundGenerationTask(continued))
      }
    }
  }
  func submit(identifier: String, device: BackgroundGenerationDevice) throws {
    guard #available(iOS 26.0, *) else { throw BackgroundGenerationSubmissionError.unavailable }
    let request = BGContinuedProcessingTaskRequest(
      identifier: identifier, title: "Génération MLX", subtitle: "Préparation du texte"
    )
    request.strategy = .fail
    request.requiredResources = device == .gpu ? .gpu : []
    do { try BGTaskScheduler.shared.submit(request) }
    catch {
      switch (error as NSError).code {
      case BGTaskScheduler.Error.Code.notPermitted.rawValue: throw BackgroundGenerationSubmissionError.notPermitted
      case BGTaskScheduler.Error.Code.immediateRunIneligible.rawValue: throw BackgroundGenerationSubmissionError.busy
      default: throw BackgroundGenerationSubmissionError.unavailable
      }
    }
  }
  func cancel(identifier: String) { BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: identifier) }
}
#endif
