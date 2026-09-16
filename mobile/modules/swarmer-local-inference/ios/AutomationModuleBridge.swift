import ExpoModulesCore
import Foundation

struct AutomationStartRecord: Record, Sendable {
  @Field var enabled: Bool = false
  @Field var port: Int? = nil
  @Field var reason: String? = nil

  init() {}

  init(enabled: Bool, port: Int? = nil, reason: String? = nil) {
    self.enabled = enabled
    self.port = port
    self.reason = reason
  }
}

#if DEBUG
import UIKit

@MainActor
final class AutomationIdleTimerPolicy {
  static let shared = AutomationIdleTimerPolicy()
  private let lease = AutomationIdleTimerLease(
    read: { UIApplication.shared.isIdleTimerDisabled },
    write: { UIApplication.shared.isIdleTimerDisabled = $0 }
  )
  private var backgroundObserver: NSObjectProtocol?

  private init() {
    backgroundObserver = NotificationCenter.default.addObserver(
      forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main
    ) { [weak self] _ in
      MainActor.assumeIsolated { self?.lease.setReady(false) }
    }
  }

  func setReady(_ ready: Bool) {
    lease.setReady(ready && UIApplication.shared.applicationState == .active)
  }
}

/// Expo's module is not Sendable. Only the weak reference is shared, under a lock;
/// event delivery always occurs on the main queue and carries immutable string values.
final class AutomationModuleEventEmitter: @unchecked Sendable {
  private let lock = NSLock()
  private weak var module: BaseModule?

  func attach(_ module: BaseModule) {
    lock.withLock { self.module = module }
  }

  func send(_ request: AutomationRequest) {
    DispatchQueue.main.async { [self] in
      let target = lock.withLock { module }
      target?.sendEvent("automationRequest", [
        "requestId": request.requestId,
        "method": request.method,
        "path": request.path,
        "body": request.body,
      ])
    }
  }
}
#endif
