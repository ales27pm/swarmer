import Foundation

private final class ReadyTransitions: @unchecked Sendable {
  private let lock = NSLock()
  private var values: [Bool] = []
  func append(_ value: Bool) { lock.withLock { values.append(value) } }
  func snapshot() -> [Bool] { lock.withLock { values } }
}

private actor TestApplication {
  private var count = 0
  func execute(_ request: AutomationRequest, server: AutomationServer) async {
    count += 1
    if request.path == "/v1/pending" { return }
    let object: [String: Any] = ["count": count, "path": request.path, "body": request.body]
    guard let bytes = try? JSONSerialization.data(withJSONObject: object),
          let body = String(data: bytes, encoding: .utf8) else { return }
    try? await server.complete(requestID: request.requestId, status: 200, body: body)
  }
}

@main
struct AutomationTransportHost {
  static func main() async {
    let server = AutomationServer()
    let application = TestApplication()
    let environment = ProcessInfo.processInfo.environment
    let transitions = ReadyTransitions()
    let emit: @Sendable (AutomationRequest) -> Void = { request in
      Task { await application.execute(request, server: server) }
    }
    func report(_ result: AutomationStartResult) {
      let object: [String: Any] = ["enabled": result.enabled, "port": result.port ?? 0, "reason": result.reason ?? "", "readyChanges": transitions.snapshot()]
      if let bytes = try? JSONSerialization.data(withJSONObject: object) {
        FileHandle.standardOutput.write(bytes + Data([10]))
      }
    }
    report(await server.start(environment: environment, foreground: true, emit: emit, onReadyChanged: { transitions.append($0) }))
    while let command = await Task.detached(operation: { readLine() }).value {
      switch command {
      case "start": report(await server.start(environment: environment, foreground: true, emit: emit, onReadyChanged: { transitions.append($0) }))
      case "background":
        await server.stop(reason: "background")
        report(.init(enabled: false, reason: "background"))
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
