import Foundation

enum CoreMLDiagnosticComputeUnits: String, CaseIterable, Sendable {
  case all, cpuOnly, cpuAndGPU, cpuAndNeuralEngine

  static func requested(_ value: String?, runtime: String) throws -> Self? {
    guard let value else { return nil }
    #if DEBUG
    guard runtime == "coreml", let units = Self(rawValue: value) else {
      throw CoreMLDiagnosticOptionError()
    }
    return units
    #else
    throw CoreMLDiagnosticOptionError()
    #endif
  }
}

struct CoreMLDiagnosticOptionError: Error, LocalizedError {
  var errorDescription: String? { "Core ML diagnostic options require a development build and a valid Core ML compute unit." }
}

struct CoreMLLoadDiagnostic: Codable, Equatable, Sendable {
  enum Stage: String, CaseIterable, Codable, Sendable {
    case resolve, validateArtifact, compile, load, validateContract, tokenizer, complete
  }
  enum Outcome: String, Codable, Sendable { case succeeded, failed, cancelled }
  struct Failure: Codable, Equatable, Sendable {
    let domain: String
    let code: Int
    let executionPlanCode: Int?
  }

  let computeUnits: String
  let stage: Stage
  let outcome: Outcome
  let elapsedMilliseconds: Double
  let errors: [Failure]
  let errorsTruncated: Bool

  // Arbitrary NSError domains and userInfo may contain paths or credentials.
  static let allowedDomains: Set<String> = [
    "com.apple.CoreML", "com.apple.appleneuralengine", "com.apple.espresso",
    "com.apple.Espresso", "com.apple.MPS", "MPSGraphErrorDomain",
    NSCocoaErrorDomain, NSPOSIXErrorDomain, NSOSStatusErrorDomain, NSURLErrorDomain,
  ]

  static func capture(
    units: CoreMLDiagnosticComputeUnits, stage: Stage, startedAt: TimeInterval,
    error: (any Error)? = nil, now: TimeInterval = ProcessInfo.processInfo.systemUptime
  ) -> Self {
    var errors: [Failure] = []
    var seen = Set<ObjectIdentifier>()
    var current = error.map { $0 as NSError }
    while let item = current, errors.count < 4, seen.insert(ObjectIdentifier(item)).inserted {
      errors.append(Failure(
        domain: allowedDomains.contains(item.domain) ? item.domain : "other",
        code: max(-2_147_483_648, min(2_147_483_647, item.code)),
        executionPlanCode: executionPlanCode(item.userInfo[NSLocalizedDescriptionKey] as? String)
      ))
      current = item.userInfo[NSUnderlyingErrorKey] as? NSError
    }
    let duration = (now - startedAt) * 1000
    return Self(
      computeUnits: units.rawValue, stage: stage,
      outcome: error == nil ? .succeeded : (error is CancellationError ? .cancelled : .failed),
      elapsedMilliseconds: duration.isFinite ? min(86_400_000, max(0, duration)) : 0,
      errors: errors, errorsTruncated: current != nil
    )
  }

  private static func executionPlanCode(_ description: String?) -> Int? {
    // Retain the numeric -14 signal even when NSError.code is 0. Never retain text.
    guard let description else { return nil }
    let bounded = String(description.prefix(4097))
    // A truncated suffix could turn an incomplete number into a different code,
    // or hide another code. Keep only unambiguous, fully bounded descriptions.
    guard bounded.count <= 4096,
          let plan = bounded.range(of: #"\bFailed to build (?:a|the) model execution plan\b"#,
                                   options: .regularExpression),
          let markers = try? NSRegularExpression(pattern: #"\berror code\b"#),
          let codes = try? NSRegularExpression(
            pattern: #"\berror code(?:[ \t]*:[ \t]*|[ \t]+)(-?[0-9]{1,9})(?=\s|\.(?:\s|$)|$)"#
          ) else { return nil }
    let bounds = NSRange(bounded.startIndex..., in: bounded)
    guard markers.numberOfMatches(in: bounded, range: bounds) == 1 else { return nil }
    let matches = codes.matches(in: bounded, range: bounds)
    guard matches.count == 1, let match = matches.first,
          let whole = Range(match.range, in: bounded), whole.lowerBound >= plan.upperBound,
          let number = Range(match.range(at: 1), in: bounded) else { return nil }
    return Int(bounded[number])
  }
}

struct CoreMLDiagnosticLoadFailure: Error, LocalizedError, Sendable {
  let diagnostic: CoreMLLoadDiagnostic
  var errorDescription: String? { "Core ML diagnostic load failed at stage \(diagnostic.stage.rawValue)." }
}
