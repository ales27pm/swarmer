#if DEBUG
import Foundation

struct AutomationRequest: Sendable {
  let requestId: String
  let method: String
  let path: String
  let body: String
}

struct AutomationConfiguration: Sendable {
  let port: UInt16
  let token: String
  let identity: Data
  let password: String
  let ttlSeconds: Int

  static func read(_ environment: [String: String]) throws -> Self? {
    guard environment["SWARMER_AUTOMATION_ENABLE"] == "1" else { return nil }
    guard let port = UInt16(environment["SWARMER_AUTOMATION_PORT"] ?? "8766"), port >= 1024,
          let token = environment["SWARMER_AUTOMATION_TOKEN"],
          (32...256).contains(token.utf8.count),
          token.utf8.allSatisfy({ (33...126).contains($0) }),
          let encoded = environment["SWARMER_AUTOMATION_TLS_P12"], encoded.utf8.count <= 65_536,
          let identity = Data(base64Encoded: encoded), !identity.isEmpty,
          let password = environment["SWARMER_AUTOMATION_TLS_PASSWORD"],
          !password.isEmpty, password.utf8.count <= 1024,
          let ttl = Int(environment["SWARMER_AUTOMATION_TTL_SECONDS"] ?? "900"),
          (1...3600).contains(ttl) else { throw AutomationProtocolError.invalidConfiguration }
    return Self(port: port, token: token, identity: identity, password: password, ttlSeconds: ttl)
  }
}

enum AutomationProtocolError: Error {
  case invalidConfiguration, invalidRequest, unauthorized, tooLarge, invalidResponse, unavailable
}

/// Deliberately small HTTP/1.1 subset. A completed request consumes the entire connection.
struct AutomationHTTPParser: Sendable {
  static let headerLimit = 8192
  static let bodyLimit = 65_536
  static let responseLimit = 1_048_576
  private var buffer = Data()
  private var completed = false

  mutating func append(_ bytes: Data, token: String) throws -> AutomationRequest? {
    guard !completed else { throw AutomationProtocolError.invalidRequest }
    guard bytes.count <= Self.headerLimit + Self.bodyLimit - buffer.count else {
      throw AutomationProtocolError.tooLarge
    }
    buffer.append(bytes)
    guard let delimiter = buffer.range(of: Data([13, 10, 13, 10])) else {
      guard buffer.count <= Self.headerLimit else { throw AutomationProtocolError.tooLarge }
      return nil
    }
    let bodyOffset = delimiter.upperBound
    guard bodyOffset <= Self.headerLimit else { throw AutomationProtocolError.tooLarge }
    let headerBytes = buffer[..<delimiter.lowerBound]
    guard headerBytes.allSatisfy({ $0 == 13 || $0 == 10 || (32...126).contains($0) }),
          let headerText = String(data: headerBytes, encoding: .ascii) else {
      throw AutomationProtocolError.invalidRequest
    }
    let lines = headerText.components(separatedBy: "\r\n")
    let requestLine = (lines.first ?? "").components(separatedBy: " ")
    guard requestLine.count == 3, ["GET", "POST"].contains(requestLine[0]),
          requestLine[2] == "HTTP/1.1", validPath(requestLine[1]) else {
      throw AutomationProtocolError.invalidRequest
    }
    var headers: [String: String] = [:]
    for line in lines.dropFirst() {
      guard let colon = line.firstIndex(of: ":"), colon != line.startIndex else {
        throw AutomationProtocolError.invalidRequest
      }
      let name = String(line[..<colon]).lowercased()
      guard name.utf8.allSatisfy({ (97...122).contains($0) || (48...57).contains($0) || $0 == 45 }),
            headers[name] == nil else { throw AutomationProtocolError.invalidRequest }
      let value = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
      guard !value.contains("\r"), !value.contains("\n") else { throw AutomationProtocolError.invalidRequest }
      headers[name] = value
    }
    guard let host = headers["host"], !host.isEmpty,
          headers["transfer-encoding"] == nil, headers["expect"] == nil,
          headers["origin"] == nil, headers["upgrade"] == nil,
          !headers.keys.contains(where: { $0.hasPrefix("access-control-") || $0.hasPrefix("sec-fetch-") }),
          headers["connection"] == nil || headers["connection"]?.lowercased() == "close" else {
      throw AutomationProtocolError.invalidRequest
    }
    guard constantTimeEqual(headers["authorization"] ?? "", "Bearer " + token) else {
      throw AutomationProtocolError.unauthorized
    }
    let length: Int
    if let raw = headers["content-length"] {
      guard !raw.isEmpty, raw.utf8.allSatisfy({ (48...57).contains($0) }), let parsed = Int(raw) else {
        throw AutomationProtocolError.invalidRequest
      }
      length = parsed
    } else {
      guard requestLine[0] == "GET" else { throw AutomationProtocolError.invalidRequest }
      length = 0
    }
    guard length <= Self.bodyLimit else { throw AutomationProtocolError.tooLarge }
    if requestLine[0] == "GET" {
      guard length == 0 else { throw AutomationProtocolError.invalidRequest }
    } else {
      guard length > 0, ["application/json", "application/json; charset=utf-8"].contains(
        headers["content-type"]?.lowercased() ?? ""
      ) else { throw AutomationProtocolError.invalidRequest }
    }
    guard buffer.count <= bodyOffset + length else { throw AutomationProtocolError.invalidRequest }
    guard buffer.count == bodyOffset + length else { return nil }
    let bodyData = buffer[bodyOffset...]
    guard let body = String(data: bodyData, encoding: .utf8) else { throw AutomationProtocolError.invalidRequest }
    if length > 0 {
      guard (try? JSONSerialization.jsonObject(with: bodyData)) != nil else {
        throw AutomationProtocolError.invalidRequest
      }
    }
    completed = true
    buffer.removeAll(keepingCapacity: false)
    return AutomationRequest(requestId: UUID().uuidString, method: requestLine[0], path: requestLine[1], body: body)
  }

  private func validPath(_ path: String) -> Bool {
    guard path.utf8.count <= 512, path.hasPrefix("/v1/"), !path.hasSuffix("/"),
          !path.contains("//") else { return false }
    return path.split(separator: "/").allSatisfy { component in
      component != "." && component != ".." && component.utf8.allSatisfy {
        (97...122).contains($0) || (65...90).contains($0) || (48...57).contains($0) || [45, 46, 95].contains($0)
      }
    }
  }

  private func constantTimeEqual(_ first: String, _ second: String) -> Bool {
    let left = Array(first.utf8), right = Array(second.utf8)
    guard left.count == right.count else { return false }
    var difference: UInt8 = 0
    for index in left.indices { difference |= left[index] ^ right[index] }
    return difference == 0
  }

  static func response(status: Int, body: String) throws -> Data {
    let bytes = Data(body.utf8)
    guard (200...599).contains(status), bytes.count <= responseLimit,
          (try? JSONSerialization.jsonObject(with: bytes, options: [.fragmentsAllowed])) != nil else {
      throw AutomationProtocolError.invalidResponse
    }
    let header = "HTTP/1.1 \(status) Response\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: \(bytes.count)\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n"
    return Data(header.utf8) + bytes
  }
}
#endif
