import Foundation

@MainActor
private final class TestExecutionAccess {
  var foreground = true
  var continuation = false
  func current() -> AutomationExecutionAccess {
    .init(foreground: foreground, backgroundContinuation: !foreground && continuation)
  }
}

@main
struct AutomationProtocolTests {
  struct Failure: Error { let message: String }
  static let token = String(repeating: "a", count: 64)

  static func expect(_ value: @autoclosure () -> Bool, _ message: String) throws {
    if !value() { throw Failure(message: message) }
  }

  static func request(_ line: String = "GET /v1/health HTTP/1.1", headers: String = "", body: String = "") -> String {
    "\(line)\r\nHost: [::1]:8766\r\nAuthorization: Bearer \(token)\r\n\(headers)\r\n\(body)"
  }

  static func rejected(_ wire: String, _ reason: String) throws {
    var parser = AutomationHTTPParser()
    do { _ = try parser.append(Data(wire.utf8), token: token) }
    catch is AutomationProtocolError { return }
    throw Failure(message: "Accepted \(reason)")
  }

  static func main() async throws {
    try await MainActor.run {
      let oldDelivery = AutomationDeliveryFence()
      var deliveries = 0
      let later = ContinuousClock.now.advanced(by: .seconds(10))
      try expect(oldDelivery.deliver(before: later) { deliveries += 1 }, "Live delivery rejected")
      oldDelivery.invalidate()
      let replacement = AutomationDeliveryFence()
      try expect(!oldDelivery.deliver(before: later) { deliveries += 1 }, "Queued old-session request survived stop/restart")
      try expect(!replacement.deliver(before: .now.advanced(by: .seconds(-1))) { deliveries += 1 }, "Expired request was emitted")
      try expect(replacement.deliver(before: later) { deliveries += 1 }, "Old invalidation stopped a new generation")
      try expect(deliveries == 2, "Rejected request reached the event emitter")
      for initial in [false, true] {
        var disabled = initial
        let lease = AutomationIdleTimerLease(read: { disabled }, write: { disabled = $0 })
        lease.setReady(false)
        try expect(disabled == initial, "Inactive automation changed the idle-timer preference")
        lease.setReady(true)
        lease.setReady(true)
        try expect(disabled, "Ready automation did not hold the idle timer")
        lease.setReady(false)
        lease.setReady(false)
        try expect(disabled == initial, "Stop did not restore the original idle-timer preference")
        disabled = !initial
        lease.setReady(true)
        lease.setReady(false)
        try expect(disabled == !initial, "A new session restored an obsolete idle-timer preference")
      }
    }
    let wire = Data(request().utf8)
    var parser = AutomationHTTPParser()
    for byte in wire.dropLast() {
      let result = try parser.append(Data([byte]), token: token)
      try expect(result == nil, "Fragmented request dispatched early")
    }
    let result = try parser.append(wire.suffix(1), token: token)
    try expect(result?.method == "GET" && result?.path == "/v1/health" && result?.body == "", "GET changed")
    do {
      _ = try parser.append(wire, token: token)
      throw Failure(message: "Parser accepted a second request")
    } catch is AutomationProtocolError {}

    let body = #"{"command":"llm.generate","text":"été 📖"}"#
    let post = request("POST /v1/commands HTTP/1.1", headers: "Content-Type: application/json\r\nContent-Length: \(body.utf8.count)\r\n", body: body)
    var postParser = AutomationHTTPParser()
    let parsedPost = try postParser.append(Data(post.utf8), token: token)
    try expect(parsedPost?.body == body && parsedPost?.method == "POST", "UTF-8 body damaged")

    try rejected(request().replacingOccurrences(of: "Bearer \(token)", with: "Bearer wrong"), "wrong bearer")
    try rejected(request().replacingOccurrences(of: "Authorization: Bearer \(token)\r\n", with: ""), "missing bearer")
    try rejected(request(headers: "authorization: Bearer \(token)\r\n"), "duplicate authorization")
    try rejected(request(headers: "Content-Length: 0\r\ncontent-length: 0\r\n"), "duplicate lengths")
    try rejected(request(headers: "Transfer-Encoding: chunked\r\n"), "chunked encoding")
    try rejected(request(headers: "Origin: https://example.invalid\r\n"), "browser origin")
    try rejected(request(headers: "Sec-Fetch-Site: cross-site\r\n"), "browser request")
    try rejected(request(headers: "Expect: 100-continue\r\n"), "expect header")
    try rejected(request(headers: "Connection: keep-alive\r\n"), "keep alive")
    try rejected(request(headers: "X-Test: a\r\n folded\r\n"), "folded header")
    try rejected(request(headers: "X-Test : a\r\n"), "header name whitespace")
    try rejected(request(headers: "X-Test: a\nb\r\n"), "bare newline")
    try rejected(request("POST /v1/commands HTTP/1.1"), "missing POST length")
    try rejected(request(headers: "Content-Length: +0\r\n"), "ambiguous numeric length")
    try rejected(request(headers: "Content-Length: 1\r\n", body: "x"), "GET body")
    try rejected(post.replacingOccurrences(of: "application/json", with: "text/plain"), "non-JSON content type")
    try rejected(request("POST /v1/commands HTTP/1.1", headers: "Content-Type: application/json\r\nContent-Length: 1\r\n", body: "{"), "invalid JSON")
    try rejected(request("POST /v1/commands HTTP/1.1", headers: "Content-Type: application/json\r\nContent-Length: 65537\r\n"), "oversize declared body")
    try rejected(request(headers: "X-Long: \(String(repeating: "x", count: 8200))\r\n"), "oversize headers")
    try rejected(request() + request(), "coalesced pipeline")
    for route in ["/v1/health?token=x", "/v1/health#x", "/v1/%68ealth", "/v1/../secret", "/v1//health", "https://example.invalid/v1/health", "/v1/"] {
      try rejected(request("GET \(route) HTTP/1.1"), "invalid path")
    }
    try rejected(request("OPTIONS /v1/health HTTP/1.1"), "CORS preflight")
    try rejected(request("GET /v1/health HTTP/1.0"), "HTTP/1.0")

    let response = try AutomationHTTPParser.response(status: 202, body: body)
    let text = String(decoding: response, as: UTF8.self)
    try expect(text.contains("Content-Length: \(body.utf8.count)\r\n"), "Response length counts characters")
    try expect(text.contains("Cache-Control: no-store") && !text.contains("Access-Control-"), "Response exposure changed")
    for (status, content) in [(101, "{}"), (600, "{}"), (200, "broken"), (200, "\"\(String(repeating: "x", count: AutomationHTTPParser.responseLimit))\"")] {
      do { _ = try AutomationHTTPParser.response(status: status, body: content); throw Failure(message: "Invalid response accepted") }
      catch is AutomationProtocolError {}
    }

    var environment = ["SWARMER_AUTOMATION_ENABLE": "1", "SWARMER_AUTOMATION_TOKEN": token,
      "SWARMER_AUTOMATION_TLS_P12": Data([1, 2, 3]).base64EncodedString(), "SWARMER_AUTOMATION_TLS_PASSWORD": "test-password"]
    let absentConfig = try AutomationConfiguration.read([:])
    try expect(absentConfig == nil, "Server enabled by default")
    let config = try AutomationConfiguration.read(environment)
    try expect(config?.port == 8766 && config?.ttlSeconds == 900, "Defaults changed")
    for (key, value) in [("SWARMER_AUTOMATION_TOKEN", "short"), ("SWARMER_AUTOMATION_PORT", "0"), ("SWARMER_AUTOMATION_TTL_SECONDS", "3601"), ("SWARMER_AUTOMATION_TTL_SECONDS", "0"), ("SWARMER_AUTOMATION_TLS_P12", "!"), ("SWARMER_AUTOMATION_TLS_PASSWORD", "")] {
      var invalid = environment
      invalid[key] = value
      do { _ = try AutomationConfiguration.read(invalid); throw Failure(message: "Invalid configuration accepted") }
      catch is AutomationProtocolError {}
    }
    let access = await MainActor.run { TestExecutionAccess() }
    let server = AutomationServer(access: { access.current() })
    let disabled = await server.start(environment: [:], emit: { _, _ in })
    try expect(!disabled.enabled && disabled.reason == "not_enabled", "Opt-in ignored")
    await MainActor.run { access.foreground = false }
    let background = await server.start(environment: environment, emit: { _, _ in })
    try expect(!background.enabled && background.reason == "foreground_required", "Background start accepted")
    await MainActor.run { access.continuation = true }
    let admittedBackground = await server.start(environment: environment, emit: { _, _ in })
    try expect(!admittedBackground.enabled && admittedBackground.reason == "foreground_required", "Lease created new listener in background")
    let absent = await server.reconcile()
    try expect(!absent.enabled, "Reconcile created a listener")
    await MainActor.run { access.foreground = true; access.continuation = false }
    environment["SWARMER_AUTOMATION_TLS_P12"] = Data([1, 2, 3]).base64EncodedString()
    let badIdentity = await server.start(environment: environment, emit: { _, _ in })
    try expect(!badIdentity.enabled && badIdentity.reason == "invalid_tls_configuration", "Invalid TLS identity accepted")
    await server.stop()
    do { try await server.complete(requestID: "absent", status: 200, body: "{}"); throw Failure(message: "Unknown request accepted") }
    catch is AutomationProtocolError {}
    print("PASS: automation framing, authentication, bounds, UTF-8, opt-in, foreground and invalid TLS")
  }
}
