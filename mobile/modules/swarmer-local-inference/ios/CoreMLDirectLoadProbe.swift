import Foundation
import CoreML
import CryptoKit

#if DEBUG
// A separate, synchronous Apple API path. The caller verifies the immutable
// bundled package and owns exclusive admission for the entire blocking call.
// Loading does not establish numerical correctness or hardware execution.
enum CoreMLDirectLoadProbe {
  struct Failure: Codable, Equatable, Sendable {
    let domain: String
    let code: Int
    let executionPlanCode: Int?
  }

  struct Report: Encodable, Sendable {
    let schemaVersion = 1
    let fixtureID: String
    let computeUnits: String
    let loadingAPI = "MLModel.init"
    let configuration = "defaults_except_computeUnits"
    var outcome = "failed"
    var stage = "resolve"
    var compileMilliseconds: Double = 0
    var loadMilliseconds: Double = 0
    var modelFileSHA256: String?
    var errors: [Failure] = []
    let stateCreated = false
    let predictionsPerformed = 0
    let computePlanRequested = false
    let hardwareExecutionMeasured = false

    private enum CodingKeys: String, CodingKey {
      case schemaVersion, fixtureID, computeUnits, loadingAPI, configuration, outcome, stage
      case compileMilliseconds, loadMilliseconds, modelFileSHA256, errors
      case stateCreated, predictionsPerformed, computePlanRequested, hardwareExecutionMeasured
    }

    func encode(to encoder: any Encoder) throws {
      var c = encoder.container(keyedBy: CodingKeys.self)
      try c.encode(schemaVersion, forKey: .schemaVersion)
      try c.encode(fixtureID, forKey: .fixtureID)
      try c.encode(computeUnits, forKey: .computeUnits)
      try c.encode(loadingAPI, forKey: .loadingAPI)
      try c.encode(configuration, forKey: .configuration)
      try c.encode(outcome, forKey: .outcome)
      try c.encode(stage, forKey: .stage)
      try c.encode(compileMilliseconds, forKey: .compileMilliseconds)
      try c.encode(loadMilliseconds, forKey: .loadMilliseconds)
      if let modelFileSHA256 { try c.encode(modelFileSHA256, forKey: .modelFileSHA256) }
      else { try c.encodeNil(forKey: .modelFileSHA256) }
      try c.encode(errors, forKey: .errors)
      try c.encode(stateCreated, forKey: .stateCreated)
      try c.encode(predictionsPerformed, forKey: .predictionsPerformed)
      try c.encode(computePlanRequested, forKey: .computePlanRequested)
      try c.encode(hardwareExecutionMeasured, forKey: .hardwareExecutionMeasured)
    }

    mutating func cancel() {
      outcome = "cancelled"
      errors = [Failure(domain: NSCocoaErrorDomain, code: NSUserCancelledError, executionPlanCode: nil)]
    }

    func json() throws -> String { String(decoding: try JSONEncoder().encode(self), as: UTF8.self) }
  }

  static func failure(fixtureID: String, computeUnits: String, error: any Error) -> Report {
    var report = Report(fixtureID: fixtureID, computeUnits: computeUnits)
    report.errors = failures(error)
    if error is CancellationError { report.cancel() }
    return report
  }

  // Injectable only at the two Apple API boundaries so host tests can verify
  // ordering, failure stages and cleanup without claiming device qualification.
  static func run(
    packageURL: URL, computeUnits: String,
    compile: (URL) throws -> URL = { try MLModel.compileModel(at: $0) },
    load: (URL, MLModelConfiguration) throws -> Void = { _ = try MLModel(contentsOf: $0, configuration: $1) },
    isCancelled: () -> Bool = { Task<Never, Never>.isCancelled }
  ) -> Report {
    var report = Report(fixtureID: packageURL.deletingPathExtension().lastPathComponent, computeUnits: computeUnits)
    var compiledURL: URL?
    var phaseStarted = ProcessInfo.processInfo.systemUptime
    defer { if let compiledURL { try? FileManager.default.removeItem(at: compiledURL) } }
    do {
      if isCancelled() { throw CancellationError() }
      let configuration = MLModelConfiguration()
      switch computeUnits {
      case "all": configuration.computeUnits = .all
      case "cpuOnly": configuration.computeUnits = .cpuOnly
      case "cpuAndGPU": configuration.computeUnits = .cpuAndGPU
      case "cpuAndNeuralEngine": configuration.computeUnits = .cpuAndNeuralEngine
      default: throw NSError(domain: NSCocoaErrorDomain, code: NSValidationErrorMinimum)
      }
      let modelURL = packageURL.appendingPathComponent("Data/com.apple.CoreML/model.mlmodel")
      let values = try modelURL.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
      guard values.isRegularFile == true, values.isSymbolicLink == false,
            let size = values.fileSize, size > 0, size <= 32 * 1024 * 1024 else {
        throw NSError(domain: NSCocoaErrorDomain, code: NSFileReadCorruptFileError)
      }
      let bytes = try Data(contentsOf: modelURL)
      guard bytes.count == size else { throw NSError(domain: NSCocoaErrorDomain, code: NSFileReadCorruptFileError) }
      report.modelFileSHA256 = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
      if isCancelled() { throw CancellationError() }
      report.stage = "compile"
      phaseStarted = ProcessInfo.processInfo.systemUptime
      let compiled = try compile(packageURL)
      // Never remove source resources, even if an API unexpectedly returns them.
      guard compiled.standardizedFileURL != packageURL.standardizedFileURL,
            compiled.pathExtension == "mlmodelc" else {
        throw NSError(domain: NSCocoaErrorDomain, code: NSFileReadCorruptFileError)
      }
      compiledURL = compiled
      report.compileMilliseconds = elapsed(phaseStarted)
      if isCancelled() { throw CancellationError() }
      report.stage = "load"
      phaseStarted = ProcessInfo.processInfo.systemUptime
      try load(compiled, configuration)
      report.loadMilliseconds = elapsed(phaseStarted)
      if isCancelled() { throw CancellationError() }
      report.stage = "complete"
      report.outcome = "loaded"
    } catch {
      if report.stage == "compile" { report.compileMilliseconds = elapsed(phaseStarted) }
      if report.stage == "load" { report.loadMilliseconds = elapsed(phaseStarted) }
      report.errors = failures(error)
      if error is CancellationError || isCancelled() { report.cancel() }
    }
    return report
  }

  private static func elapsed(_ started: TimeInterval) -> Double {
    let value = (ProcessInfo.processInfo.systemUptime - started) * 1000
    return value.isFinite ? min(86_400_000, max(0, value)) : 0
  }

  private static func failures(_ error: any Error) -> [Failure] {
    let allowed: Set<String> = [
      "com.apple.CoreML", "com.apple.appleneuralengine", "com.apple.espresso", "com.apple.Espresso",
      "com.apple.MPS", "MPSGraphErrorDomain", NSCocoaErrorDomain, NSPOSIXErrorDomain,
      NSOSStatusErrorDomain, NSURLErrorDomain,
    ]
    var result: [Failure] = []
    var seen = Set<ObjectIdentifier>()
    var current: NSError? = error as NSError
    while let item = current, result.count < 4, seen.insert(ObjectIdentifier(item)).inserted {
      result.append(Failure(domain: allowed.contains(item.domain) ? item.domain : "other",
                            code: max(-2_147_483_648, min(2_147_483_647, item.code)),
                            executionPlanCode: executionPlanCode(item.userInfo[NSLocalizedDescriptionKey] as? String)))
      current = item.userInfo[NSUnderlyingErrorKey] as? NSError
    }
    return result
  }

  private static func executionPlanCode(_ description: String?) -> Int? {
    guard let description else { return nil }
    let bounded = String(description.prefix(4097))
    guard bounded.count <= 4096,
          let plan = bounded.range(of: #"\bFailed to build (?:a|the) model execution plan\b"#, options: .regularExpression),
          let markers = try? NSRegularExpression(pattern: #"\berror code\b"#),
          let codes = try? NSRegularExpression(pattern: #"\berror code(?:[ \t]*:[ \t]*|[ \t]+)(-?[0-9]{1,9})(?=\s|\.(?:\s|$)|$)"#)
    else { return nil }
    let bounds = NSRange(bounded.startIndex..., in: bounded)
    guard markers.numberOfMatches(in: bounded, range: bounds) == 1 else { return nil }
    let matches = codes.matches(in: bounded, range: bounds)
    guard matches.count == 1, let match = matches.first,
          let whole = Range(match.range, in: bounded), whole.lowerBound >= plan.upperBound,
          let number = Range(match.range(at: 1), in: bounded) else { return nil }
    return Int(bounded[number])
  }
}
#endif
