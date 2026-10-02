import Foundation

private final class LinkedError: NSError, @unchecked Sendable {
  var next: NSError?
  override var userInfo: [String: Any] { next.map { [NSUnderlyingErrorKey: $0] } ?? [:] }
}

@main
struct CoreMLLoadDiagnosticsTests {
  private struct Failure: Error { let message: String }
  private static func expect(_ result: Bool, _ message: String) throws {
    if !result { throw Failure(message: message) }
  }
  private static func reject(_ value: String, runtime: String = "coreml") throws {
    do { _ = try CoreMLDiagnosticComputeUnits.requested(value, runtime: runtime) }
    catch is CoreMLDiagnosticOptionError { return }
    throw Failure(message: "Diagnostic option was accepted")
  }
  static func main() throws {
    try expect(try CoreMLDiagnosticComputeUnits.requested(nil, runtime: "mlx") == nil,
               "Absent option must preserve the default runtime")
    #if DEBUG
    for value in CoreMLDiagnosticComputeUnits.allCases {
      try expect(try CoreMLDiagnosticComputeUnits.requested(value.rawValue, runtime: "coreml") == value,
                 "Explicit unit was changed")
      try reject(value.rawValue, runtime: "mlx")
    }
    #else
    for value in CoreMLDiagnosticComputeUnits.allCases { try reject(value.rawValue) }
    #endif
    try reject("aneOnly")
    try reject("all ")

    let privateError = NSError(domain: "/private/token-secret", code: -14, userInfo: [
      NSLocalizedDescriptionKey: "Failed to build the model execution plan using /private/model.mil with error code: -14.",
      NSLocalizedFailureReasonErrorKey: "password-secret", "token": "token-secret",
    ])
    let outer = NSError(domain: "com.apple.CoreML", code: 0, userInfo: [NSUnderlyingErrorKey: privateError])
    let receipt = CoreMLLoadDiagnostic.capture(units: .cpuAndNeuralEngine, stage: .load,
                                              startedAt: 1, error: outer, now: 2.25)
    try expect(receipt.elapsedMilliseconds == 1250 && receipt.errors.count == 2, "Timing or underlying errors lost")
    try expect(receipt.errors[1].domain == "other" && receipt.errors[1].code == -14
      && receipt.errors[1].executionPlanCode == -14, "Unknown domain must redact only text, retaining numeric evidence")
    let json = String(decoding: try JSONEncoder().encode(receipt), as: UTF8.self)
    try expect(!json.contains("private") && !json.contains("secret") && !json.contains("model.mil"), "Private NSError data escaped")
    try expect(receipt.outcome == .failed && !receipt.errorsTruncated, "Wrong failure outcome")

    for article in ["a", "the"] {
      for separator in [": ", " "] {
        let message = "Failed to build \(article) model execution plan using /private/model.mil with error code\(separator)-14."
        let failure = NSError(domain: "com.apple.CoreML", code: 0,
                              userInfo: [NSLocalizedDescriptionKey: message])
        let result = CoreMLLoadDiagnostic.capture(units: .cpuAndNeuralEngine, stage: .load,
                                                 startedAt: 0, error: failure, now: 1)
        try expect(result.errors.first?.executionPlanCode == -14,
                   "Native execution-plan wording lost its numeric subcode")
        let encoded = String(decoding: try JSONEncoder().encode(result), as: UTF8.self)
        try expect(!encoded.contains("private") && !encoded.contains("model.mil"),
                   "Execution-plan wording escaped into the diagnostic")
      }
    }
    for message in [
      "error code -14 without an execution-plan failure",
      "Failed to build some model execution plan with error code -14.",
      "Failed to build a model execution plan with error code --14.",
      "Failed to build a model execution plan with error code -1400000000.",
      "Failed to build a model execution plan with error code -14private.",
      "Failed to build a model execution plan with error code -14.5.",
      "Failed to build a model execution plan with error code -14. Underlying error code -8.",
      "Failed to build a model execution plan with error code -1400000000. Underlying error code -14.",
      String(repeating: "x", count: 4096) + "Failed to build a model execution plan with error code -14.",
      "Failed to build a model execution plan with error code -14." + String(repeating: "x", count: 4096),
    ] {
      let failure = NSError(domain: "com.apple.CoreML", code: 0,
                            userInfo: [NSLocalizedDescriptionKey: message])
      let result = CoreMLLoadDiagnostic.capture(units: .all, stage: .load,
                                               startedAt: 0, error: failure, now: 1)
      try expect(result.errors.first?.executionPlanCode == nil,
                 "Ambiguous, oversized or malformed wording invented a subcode")
    }

    let first = LinkedError(domain: NSCocoaErrorDomain, code: 1)
    let second = LinkedError(domain: NSCocoaErrorDomain, code: 2)
    first.next = second; second.next = first
    let cycle = CoreMLLoadDiagnostic.capture(units: .all, stage: .compile, startedAt: 0, error: first, now: 1)
    try expect(cycle.errors.count == 2 && cycle.errorsTruncated, "Underlying error cycle must stop")
    first.next = nil; second.next = nil
    var deep = NSError(domain: NSPOSIXErrorDomain, code: 1)
    for code in 2...7 { deep = NSError(domain: NSPOSIXErrorDomain, code: code, userInfo: [NSUnderlyingErrorKey: deep]) }
    let capped = CoreMLLoadDiagnostic.capture(units: .all, stage: .compile, startedAt: 0, error: deep, now: 1)
    try expect(capped.errors.count == 4 && capped.errorsTruncated, "Deep chain exceeded bound")
    for stage in CoreMLLoadDiagnostic.Stage.allCases {
      let failure = CoreMLLoadDiagnostic.capture(units: .cpuOnly, stage: stage, startedAt: 2, error: outer, now: 1)
      try expect(failure.stage == stage && failure.elapsedMilliseconds == 0, "Failure stage or timing bound changed")
    }
    let cancelled = CoreMLLoadDiagnostic.capture(units: .cpuAndGPU, stage: .load, startedAt: 0, error: CancellationError(), now: 1)
    try expect(cancelled.outcome == .cancelled, "Cancellation became a load failure")
    let success = CoreMLLoadDiagnostic.capture(units: .all, stage: .complete, startedAt: 0, now: .infinity)
    try expect(success.outcome == .succeeded && success.errors.isEmpty && success.elapsedMilliseconds == 0,
               "Successful receipt invented errors or unbounded timing")
    print("PASS: Core ML diagnostic policy, stage, cancellation, privacy, cycle and depth bounds")
  }
}
