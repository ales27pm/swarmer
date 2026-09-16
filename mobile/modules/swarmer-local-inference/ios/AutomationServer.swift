#if DEBUG
import Foundation
import Network
import Security

struct AutomationStartResult: Sendable {
  let enabled: Bool
  var port: Int? = nil
  var reason: String? = nil
}

/// Background execution belongs to admitted finite work, not to this listener.
struct AutomationExecutionAccess: Sendable {
  let foreground: Bool
  let backgroundContinuation: Bool
  var mayServe: Bool { foreground || backgroundContinuation }
}

/// A queued MainActor delivery cannot outlive stop/restart or the session TTL.
/// The lock covers the synchronous event emission, so invalidation is a barrier.
final class AutomationDeliveryFence: @unchecked Sendable {
  private let lock = NSLock()
  private var valid = true

  func invalidate() { lock.withLock { valid = false } }

  @MainActor
  func deliver(before deadline: ContinuousClock.Instant, _ action: () -> Void) -> Bool {
    lock.withLock {
      guard valid, ContinuousClock.now < deadline else { return false }
      action()
      return true
    }
  }
}

/// The app's original idle-timer preference belongs to its owner, not to automation.
@MainActor
final class AutomationIdleTimerLease {
  private let read: () -> Bool
  private let write: (Bool) -> Void
  private var previous: Bool?

  init(read: @escaping () -> Bool, write: @escaping (Bool) -> Void) {
    self.read = read
    self.write = write
  }

  func setReady(_ ready: Bool) {
    if ready {
      if previous == nil { previous = read() }
      write(true)
    } else if let previous {
      write(previous)
      self.previous = nil
    }
  }
}

/// TLS and connection ownership are isolated here; business operations are dispatched to the shared JS API.
actor AutomationServer {
  private struct Client {
    let connection: NWConnection
    var parser = AutomationHTTPParser()
    var requestID: String?
    var timer: Task<Void, Never>?
  }
  private let queue = DispatchQueue(label: "org.27pm.mongars.automation")
  private var listener: NWListener?
  private var clients: [UUID: Client] = [:]
  private var pending: [String: UUID] = [:]
  private var configuration: AutomationConfiguration?
  private var deadline: ContinuousClock.Instant?
  private var expiration: Task<Void, Never>?
  private var startupTimer: Task<Void, Never>?
  private var startup: CheckedContinuation<AutomationStartResult, Never>?
  private var emit: (@MainActor @Sendable (AutomationRequest, AutomationExecutionAccess) -> Void)?
  private let readAccess: @MainActor @Sendable () -> AutomationExecutionAccess
  private var generation = UUID()
  private var deliveryFence = AutomationDeliveryFence()
  private var readyPort: Int?
  private var readinessChanged: (@Sendable (Bool) -> Void)?

  init(access: @escaping @MainActor @Sendable () -> AutomationExecutionAccess) {
    readAccess = access
  }

  func start(environment: [String: String],
             emit: @escaping @MainActor @Sendable (AutomationRequest, AutomationExecutionAccess) -> Void,
             onReadyChanged: @escaping @Sendable (Bool) -> Void = { _ in }) async -> AutomationStartResult {
    let access = await readAccess()
    if let deadline, ContinuousClock.now >= deadline {
      stop(reason: "session_expired")
      return .init(enabled: false, reason: "session_expired")
    }
    if let readyPort, access.mayServe { return .init(enabled: true, port: readyPort) }
    guard access.foreground else {
      if !access.mayServe { stop(reason: "foreground_required") }
      return .init(enabled: false, reason: "foreground_required")
    }
    guard listener == nil else { return .init(enabled: false, reason: "starting") }
    let config: AutomationConfiguration
    do {
      if let existing = configuration { config = existing }
      else {
        guard let value = try AutomationConfiguration.read(environment) else {
          return .init(enabled: false, reason: "not_enabled")
        }
        config = value
      }
      let identity = try Self.identity(config)
      let tls = NWProtocolTLS.Options()
      sec_protocol_options_set_min_tls_protocol_version(tls.securityProtocolOptions, .TLSv12)
      sec_protocol_options_set_local_identity(tls.securityProtocolOptions, identity)
      sec_protocol_options_add_tls_application_protocol(tls.securityProtocolOptions, "http/1.1")
      let parameters = NWParameters(tls: tls, tcp: NWProtocolTCP.Options())
      let listener = try NWListener(using: parameters, on: NWEndpoint.Port(rawValue: config.port)!)
      configuration = config
      if deadline == nil { deadline = ContinuousClock.now.advanced(by: .seconds(config.ttlSeconds)) }
      self.listener = listener
      self.emit = emit
      readinessChanged = onReadyChanged
      generation = UUID()
      deliveryFence = AutomationDeliveryFence()
      let current = generation
      listener.newConnectionHandler = { [weak self] connection in
        Task { await self?.accept(connection, generation: current) }
      }
      listener.stateUpdateHandler = { [weak self] state in
        Task { await self?.listenerChanged(state, generation: current) }
      }
      expiration = Task { [weak self, deadline] in
        guard let deadline else { return }
        do { try await ContinuousClock().sleep(until: deadline) } catch { return }
        await self?.stop(reason: "session_expired")
      }
      return await withCheckedContinuation { continuation in
        startup = continuation
        startupTimer = Task { [weak self] in
          do { try await Task.sleep(for: .seconds(10)) } catch { return }
          await self?.stopIfCurrent(current, reason: "listener_timeout")
        }
        // Creating a listener is foreground-only. Read UIKit again on MainActor
        // at the actual start, after TLS setup and any intervening transition.
        Task { @MainActor [readAccess, queue] in
          guard readAccess().foreground else {
            await self.stopIfCurrent(current, reason: "foreground_required")
            return
          }
          listener.start(queue: queue)
        }
      }
    } catch {
      return .init(enabled: false, reason: "invalid_tls_configuration")
    }
  }

  /// Lifecycle notifications carry no authority: use the current OS/lease state.
  /// In particular, a delayed background callback must not stop a resumed app.
  func reconcile() async -> AutomationStartResult {
    let current = generation
    let access = await readAccess()
    guard generation == current else {
      return .init(enabled: readyPort != nil, port: readyPort, reason: readyPort == nil ? "stopped" : nil)
    }
    guard access.mayServe else {
      stop(reason: "background")
      return .init(enabled: false, reason: "background")
    }
    return .init(enabled: readyPort != nil, port: readyPort, reason: readyPort == nil ? "stopped" : nil)
  }

  func stop(reason: String = "stopped") {
    deliveryFence.invalidate()
    generation = UUID()
    listener?.cancel()
    listener = nil
    if readyPort != nil { readinessChanged?(false) }
    readyPort = nil
    readinessChanged = nil
    expiration?.cancel()
    expiration = nil
    startupTimer?.cancel()
    startupTimer = nil
    startup?.resume(returning: .init(enabled: false, reason: reason))
    startup = nil
    for client in clients.values { client.timer?.cancel(); client.connection.cancel() }
    clients.removeAll()
    pending.removeAll()
    emit = nil
  }

  func complete(requestID: String, status: Int, body: String) async throws {
    let current = generation
    let access = await readAccess()
    guard generation == current else { throw AutomationProtocolError.unavailable }
    guard access.mayServe else {
      stop(reason: "background")
      throw AutomationProtocolError.unavailable
    }
    guard let deadline, ContinuousClock.now < deadline else {
      stop(reason: "session_expired")
      throw AutomationProtocolError.unavailable
    }
    guard let id = pending[requestID], clients[id]?.requestID == requestID else {
      throw AutomationProtocolError.unavailable
    }
    let response = try AutomationHTTPParser.response(status: status, body: body)
    send(response, to: id)
  }

  private static func identity(_ configuration: AutomationConfiguration) throws -> sec_identity_t {
    var result: CFArray?
    let options: [String: Any] = [
      kSecImportExportPassphrase as String: configuration.password,
      kSecImportToMemoryOnly as String: true,
    ]
    let status = SecPKCS12Import(configuration.identity as CFData, options as CFDictionary, &result)
    guard status == errSecSuccess, let items = result as? [[String: Any]], items.count == 1,
          let raw = items[0][kSecImportItemIdentity as String],
          CFGetTypeID(raw as CFTypeRef) == SecIdentityGetTypeID() else {
      throw AutomationProtocolError.invalidConfiguration
    }
    let identity = raw as! SecIdentity
    guard let wrapped = sec_identity_create(identity) else { throw AutomationProtocolError.invalidConfiguration }
    return wrapped
  }

  private func listenerChanged(_ state: NWListener.State, generation expected: UUID) async {
    let access = await readAccess()
    guard generation == expected else { return }
    guard access.mayServe else { stop(reason: "background"); return }
    switch state {
    case .ready:
      readyPort = listener?.port.map { Int($0.rawValue) }
      if readyPort != nil { readinessChanged?(true) }
      startupTimer?.cancel()
      startupTimer = nil
      startup?.resume(returning: .init(enabled: readyPort != nil, port: readyPort))
      startup = nil
    case .failed, .cancelled: stop(reason: "listener_unavailable")
    default: break
    }
  }

  private func stopIfCurrent(_ expected: UUID, reason: String) {
    guard generation == expected else { return }
    stop(reason: reason)
  }

  private func accept(_ connection: NWConnection, generation expected: UUID) async {
    let access = await readAccess()
    guard access.mayServe else {
      if generation == expected { stop(reason: "background") }
      connection.cancel()
      return
    }
    guard generation == expected, readyPort != nil, clients.count < 4,
          let deadline, ContinuousClock.now < deadline else { connection.cancel(); return }
    let id = UUID()
    clients[id] = Client(connection: connection)
    connection.stateUpdateHandler = { [weak self] state in
      switch state {
      case .failed, .cancelled: Task { await self?.close(id) }
      default: break
      }
    }
    clients[id]?.timer = timeout(id, seconds: 10)
    connection.start(queue: queue)
    receive(id)
  }

  private func receive(_ id: UUID) {
    clients[id]?.connection.receive(minimumIncompleteLength: 1, maximumLength: 8192) { [weak self] data, _, ended, error in
      Task { await self?.received(id, data: data, ended: ended, failed: error != nil) }
    }
  }

  private func received(_ id: UUID, data: Data?, ended: Bool, failed: Bool) async {
    guard var client = clients[id], client.requestID == nil, let configuration else { return }
    guard let deadline, ContinuousClock.now < deadline else { stop(reason: "session_expired"); return }
    guard !failed, let data, !data.isEmpty else { close(id); return }
    do {
      let request = try client.parser.append(data, token: configuration.token)
      clients[id] = client
      if let request {
        client.timer?.cancel()
        clients[id]?.requestID = request.requestId
        pending[request.requestId] = id
        clients[id]?.timer = timeout(id, seconds: 120)
        let current = generation
        let delivered = await MainActor.run { [readAccess, emit, deliveryFence] in
          let access = readAccess()
          guard access.mayServe, let emit else { return false }
          // The emitter runs synchronously here, with fresh access evidence.
          return deliveryFence.deliver(before: deadline) { emit(request, access) }
        }
        if !delivered, generation == current {
          stop(reason: ContinuousClock.now >= deadline ? "session_expired" : "background")
        }
      } else if ended { close(id) }
      else { receive(id) }
    } catch {
      let status: Int
      switch error {
      case AutomationProtocolError.unauthorized: status = 401
      case AutomationProtocolError.tooLarge: status = 413
      default: status = 400
      }
      if let response = try? AutomationHTTPParser.response(status: status, body: "{\"error\":\"request_rejected\"}") {
        send(response, to: id)
      } else { close(id) }
    }
  }

  private func timeout(_ id: UUID, seconds: Int) -> Task<Void, Never> {
    Task { [weak self] in
      do { try await Task.sleep(for: .seconds(seconds)) } catch { return }
      await self?.close(id)
    }
  }

  private func send(_ response: Data, to id: UUID) {
    guard let client = clients[id] else { return }
    if let requestID = client.requestID { pending.removeValue(forKey: requestID) }
    client.timer?.cancel()
    clients[id]?.timer = timeout(id, seconds: 10)
    client.connection.send(content: response, completion: .contentProcessed { [weak self] _ in
      Task { await self?.close(id) }
    })
  }

  private func close(_ id: UUID) {
    guard let client = clients.removeValue(forKey: id) else { return }
    if let requestID = client.requestID { pending.removeValue(forKey: requestID) }
    client.timer?.cancel()
    client.connection.cancel()
  }
}
#endif
