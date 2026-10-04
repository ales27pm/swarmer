import Foundation
import CoreML

@main
struct CoreMLDirectLoadProbeTests {
  private struct Failure: Error { let message: String }
  private static func expect(_ value: Bool, _ message: String) throws {
    if !value { throw Failure(message: message) }
  }

  static func main() throws {
    #if DEBUG
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    let package = root.appendingPathComponent("fixture.mlpackage")
    let model = package.appendingPathComponent("Data/com.apple.CoreML/model.mlmodel")
    let compiled = root.appendingPathComponent("fixture.mlmodelc")
    try FileManager.default.createDirectory(at: model.deletingLastPathComponent(), withIntermediateDirectories: true)
    try Data("model".utf8).write(to: model)
    defer { try? FileManager.default.removeItem(at: root) }
    var calls: [String] = []
    let compile: (URL) throws -> URL = { url in
      try expect(url == package, "Compiler did not receive the admitted package")
      calls.append("compile")
      try FileManager.default.createDirectory(at: compiled, withIntermediateDirectories: true)
      return compiled
    }
    let load: (URL, MLModelConfiguration) throws -> Void = { url, config in
      try expect(url == compiled && FileManager.default.fileExists(atPath: url.path), "Compiled model removed before loading")
      try expect(config.computeUnits == .cpuAndNeuralEngine, "Requested units were changed")
      calls.append("load")
    }
    let success = CoreMLDirectLoadProbe.run(packageURL: package, computeUnits: "cpuAndNeuralEngine", compile: compile, load: load)
    try expect(calls == ["compile", "load"] && success.outcome == "loaded" && success.stage == "complete", "Wrong Apple API sequence")
    try expect(success.modelFileSHA256 == "9372c470eeadd5ecd9c3c74c2b3cb633f8e2f2fad799250a0f70d652b6b825e4", "Wrong source digest")
    try expect(!FileManager.default.fileExists(atPath: compiled.path) && FileManager.default.fileExists(atPath: model.path), "Cleanup removed source or leaked compiled model")
    try expect(!success.stateCreated && success.predictionsPerformed == 0 && !success.computePlanRequested && !success.hardwareExecutionMeasured,
               "Load success was reported as execution")

    calls = []
    let compileFailure = CoreMLDirectLoadProbe.run(packageURL: package, computeUnits: "cpuOnly", compile: { _ in
      throw NSError(domain: "com.apple.CoreML", code: 7)
    }, load: load)
    try expect(compileFailure.stage == "compile" && compileFailure.outcome == "failed" && calls.isEmpty, "Compile failure entered load")

    let error = NSError(domain: "com.apple.CoreML", code: 0, userInfo: [
      NSLocalizedDescriptionKey: "Failed to build the model execution plan using /private/token with error code: -14.",
      NSUnderlyingErrorKey: NSError(domain: "/private/secret", code: -14),
    ])
    let failed = CoreMLDirectLoadProbe.run(packageURL: package, computeUnits: "cpuOnly", compile: compile, load: { _, _ in throw error })
    try expect(failed.stage == "load" && failed.outcome == "failed" && failed.errors[0].executionPlanCode == -14, "Lost load stage or numeric -14")
    try expect(failed.errors.count == 2 && failed.errors[1].domain == "other", "Underlying error not safely retained")
    let encoded = try failed.json()
    try expect(!encoded.contains("private") && !encoded.contains("secret") && !encoded.contains("token"), "Private NSError text escaped")
    try expect(!FileManager.default.fileExists(atPath: compiled.path), "Failure leaked compiled resource")

    var cancelled = false
    let race = CoreMLDirectLoadProbe.run(packageURL: package, computeUnits: "cpuAndNeuralEngine", compile: compile,
      load: { url, config in try load(url, config); cancelled = true }, isCancelled: { cancelled })
    try expect(race.outcome == "cancelled" && race.stage == "load" && race.errors.count == 1, "Raced cancellation published load success")
    try expect(!FileManager.default.fileExists(atPath: compiled.path), "Cancellation leaked compiled resource")
    calls = []
    let early = CoreMLDirectLoadProbe.run(packageURL: package, computeUnits: "cpuOnly", compile: compile, load: load, isCancelled: { true })
    try expect(early.outcome == "cancelled" && early.stage == "resolve" && calls.isEmpty, "Early cancellation entered Core ML")
    let earlyJSON = try JSONSerialization.jsonObject(with: Data(early.json().utf8)) as! [String: Any]
    try expect(earlyJSON["modelFileSHA256"] is NSNull, "Absent hash must be explicitly null")
    let badUnits = CoreMLDirectLoadProbe.run(packageURL: package, computeUnits: "aneOnly", compile: compile, load: load)
    try expect(badUnits.stage == "resolve" && badUnits.outcome == "failed" && calls.isEmpty, "Invalid units entered Core ML")
    let badReturn = CoreMLDirectLoadProbe.run(packageURL: package, computeUnits: "cpuOnly", compile: { _ in package }, load: load)
    try expect(badReturn.stage == "compile" && badReturn.outcome == "failed" && FileManager.default.fileExists(atPath: model.path), "Source cleanup guard failed")
    print("PASS: direct load API boundaries, source hash, stages, -14 privacy, cancellation and cleanup; host doubles only")
    #else
    print("PASS: direct load diagnostic excluded from Release")
    #endif
  }
}
