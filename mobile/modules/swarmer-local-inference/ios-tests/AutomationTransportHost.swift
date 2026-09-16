import Foundation

@MainActor
private final class TestExecutionAccess {
  var foreground = true
  var continuation = false
  var foregroundReadsRemaining: Int?
  func current() -> AutomationExecutionAccess {
    if let remaining = foregroundReadsRemaining {
      foreground = remaining > 0
      foregroundReadsRemaining = max(0, remaining - 1)
    }
    return .init(foreground: foreground, backgroundContinuation: !foreground && continuation)
  }
  func set(foreground: Bool, continuation: Bool) {
    self.foreground = foreground
    self.continuation = continuation
    foregroundReadsRemaining = nil
  }
}

private final class ReadyTransitions: @unchecked Sendable {
  private let lock = NSLock()
  private var values: [Bool] = []
  func append(_ value: Bool) { lock.withLock { values.append(value) } }
  func snapshot() -> [Bool] { lock.withLock { values } }
}

private actor TestApplication {
  private var count = 0
  func execute(_ request: AutomationRequest, access: AutomationExecutionAccess, server: AutomationServer) async {
    count += 1
    if request.path == "/v1/pending" { return }
    let object: [String: Any] = ["count": count, "path": request.path, "body": request.body,
                               "foreground": access.foreground, "backgroundContinuation": access.backgroundContinuation]
    guard let bytes = try? JSONSerialization.data(withJSONObject: object),
          let body = String(data: bytes, encoding: .utf8) else { return }
    try? await server.complete(requestID: request.requestId, status: 200, body: body)
  }
}

@main
struct AutomationTransportHost {
  static func main() async {
    let access = await MainActor.run { TestExecutionAccess() }
    let server = AutomationServer(access: { access.current() })
    let application = TestApplication()
    let environment = ProcessInfo.processInfo.environment
    let transitions = ReadyTransitions()
    let emit: @MainActor @Sendable (AutomationRequest, AutomationExecutionAccess) -> Void = { request, permission in
      Task { await application.execute(request, access: permission, server: server) }
    }
    func report(_ result: AutomationStartResult) {
      let object: [String: Any] = ["enabled": result.enabled, "port": result.port ?? 0, "reason": result.reason ?? "", "readyChanges": transitions.snapshot()]
      if let bytes = try? JSONSerialization.data(withJSONObject: object) {
        FileHandle.standardOutput.write(bytes + Data([10]))
      }
    }
    report(await server.start(environment: environment, emit: emit, onReadyChanged: { transitions.append($0) }))
    while let command = await Task.detached(operation: { readLine() }).value {
      switch command {
      case "start": report(await server.start(environment: environment, emit: emit, onReadyChanged: { transitions.append($0) }))
      case "foreground", "delayed-background-callback":
        await MainActor.run { access.set(foreground: true, continuation: false) }
        report(await server.reconcile())
      case "background-admitted":
        await MainActor.run { access.set(foreground: false, continuation: true) }
        report(await server.reconcile())
      case "lease-ended":
        await MainActor.run { access.set(foreground: false, continuation: false) }
        report(await server.reconcile())
      case "revoke-without-callback":
        await MainActor.run { access.set(foreground: false, continuation: false) }
        report(.init(enabled: false, reason: "revoked"))
      case "start-transition":
        await MainActor.run {
          access.set(foreground: true, continuation: false)
          access.foregroundReadsRemaining = 1
        }
        report(await server.start(environment: environment, emit: emit, onReadyChanged: { transitions.append($0) }))
      case "background":
        await MainActor.run { access.set(foreground: false, continuation: false) }
        report(await server.reconcile())
      case "stop":
        await server.stop()
        report(.init(enabled: false, reason: "stopped"))
      default:
        await server.stop()
        return
      }
    }
    await server.stop()
  }
}
