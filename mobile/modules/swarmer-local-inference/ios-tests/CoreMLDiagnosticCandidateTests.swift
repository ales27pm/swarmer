import CryptoKit
import Darwin
import Foundation

private struct TestFailure: Error { let message: String }
private func expect(_ condition: Bool, _ message: String) throws {
  if !condition { throw TestFailure(message: message) }
}

private struct Fixture: Sendable {
  let root: URL
  let documents: URL
  let source: URL
  let support: URL
  let weights: URL
  let manifestData: Data

  init() throws {
    root = FileManager.default.temporaryDirectory.appendingPathComponent("coreml-import-\(UUID())")
    documents = root.appendingPathComponent("Documents")
    source = documents.appendingPathComponent(CoreMLDiagnosticCandidate.directoryName)
    support = root.appendingPathComponent("ApplicationSupport")
    weights = source.appendingPathComponent("Candidate.mlpackage/Data/weights.bin")
    try FileManager.default.createDirectory(at: weights.deletingLastPathComponent(), withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
    let files: [(String, Data)] = [
      ("Candidate.mlpackage/Data/weights.bin", Data(repeating: 0x51, count: 4 * 1_024 * 1_024 + 37)),
      ("Candidate.mlpackage/Manifest.json", Data("{}".utf8)),
      ("tokenizer.json", Data("{}".utf8)), ("tokenizer_config.json", Data("{}".utf8))
    ]
    for (path, data) in files { try data.write(to: source.appendingPathComponent(path)) }
    manifestData = try JSONSerialization.data(withJSONObject: [
      "schemaVersion": 1, "candidateId": "candidate-v1", "displayName": "Diagnostic candidate",
      "modelDirectory": "Candidate.mlpackage", "files": files.map { path, data in
        ["path": path, "sizeBytes": data.count, "sha256": SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()]
      }
    ])
  }
  var manifest: CoreMLDiagnosticCandidate.Manifest { get throws { try CoreMLDiagnosticCandidate.decodeManifest(manifestData) } }
  var store: LocalModelStore { LocalModelStore(applicationSupportURL: support, documentsURL: documents) }
  var models: URL { support.appendingPathComponent("SwarmerLocalInference/Models") }
  func cleanup() { try? FileManager.default.removeItem(at: root) }
}

@main
private struct CoreMLDiagnosticCandidateTests {
  static func main() async {
    let tests: [(String, @Sendable () async throws -> Void)] = [
      ("manifest bounds, paths, hashes and required tokenizer", testManifest),
      ("multi-chunk import has a new ID and preserves existing model", testImport),
      ("missing, extra and corrupt candidate files are refused", testInventory),
      ("symlink root, parent and leaf are refused", testSymlinks),
      ("verified source mutation before copy cannot update the registry", testMutation),
      ("cancellation before copying preserves index and removes staging", testCancellation),
      ("capability remains off without a signed bundle manifest", testCapability)
    ]
    var failed = 0
    for (name, test) in tests {
      do { try await test(); print("PASS: \(name)") }
      catch { failed += 1; print("FAIL: \(name): \(error)") }
    }
    print("\(tests.count - failed)/\(tests.count) Core ML diagnostic import tests passed")
    if failed != 0 { exit(1) }
  }

  private static func testManifest() async throws {
    let fixture = try Fixture(); defer { fixture.cleanup() }
    _ = try fixture.manifest
    let original = try JSONSerialization.jsonObject(with: fixture.manifestData) as! [String: Any]
    for path in ["../secret", "/etc/passwd", "Candidate.mlpackage//x", "Candidate.mlpackage/../x", "notes.txt", "Other.mlpackage/a", "Candidate.mlpackage/a\\b", "Candidate.mlpackage/a:b"] {
      var object = original
      var files = object["files"] as! [[String: Any]]
      files[0]["path"] = path; object["files"] = files
      try rejectManifest(object)
    }
    for (key, value) in [("schemaVersion", 2 as Any), ("candidateId", "../bad" as Any), ("modelDirectory", "Candidate.mlmodelc" as Any), ("displayName", "\n" as Any)] {
      var object = original; object[key] = value; try rejectManifest(object)
    }
    for (key, value) in [("sizeBytes", -1 as Any), ("sizeBytes", CoreMLDiagnosticCandidate.maximumBytes + 1 as Any), ("sha256", "BAD" as Any)] {
      var object = original; var files = object["files"] as! [[String: Any]]
      files[0][key] = value; object["files"] = files; try rejectManifest(object)
    }
    var object = original; var files = object["files"] as! [[String: Any]]
    files.append(files[0]); object["files"] = files; try rejectManifest(object)
    object = original; object["files"] = (original["files"] as! [[String: Any]]).dropLast().map { $0 }; try rejectManifest(object)
    do {
      _ = try CoreMLDiagnosticCandidate.decodeManifest(Data(repeating: 32, count: CoreMLDiagnosticCandidate.maximumManifestBytes + 1))
      throw TestFailure(message: "oversized manifest accepted")
    } catch CoreMLDiagnosticCandidate.Failure.invalidManifest { }
  }

  private static func rejectManifest(_ object: [String: Any]) throws {
    do {
      _ = try CoreMLDiagnosticCandidate.decodeManifest(JSONSerialization.data(withJSONObject: object))
      throw TestFailure(message: "invalid manifest accepted")
    } catch CoreMLDiagnosticCandidate.Failure.invalidManifest { }
  }

  private static func testImport() async throws {
    let fixture = try Fixture(); defer { fixture.cleanup() }
    let store = fixture.store
    let first = try await store.importModel(runtime: .coreML, uri: fixture.source.absoluteString, displayName: "Original")
    let recordsBefore = try await store.list()
    let original = try await store.resolve(modelId: first.modelId)
    let before = try Data(contentsOf: original.runtimeURL.appendingPathComponent("Data/weights.bin"))
    let second = try await store.importCoreMLDiagnosticCandidate(manifest: fixture.manifest)
    let resolved = try await store.resolve(modelId: second.modelId)
    try expect(first.modelId != second.modelId, "diagnostic import overwrote identity")
    try expect(second.runtime == .coreML && second.displayName == "Diagnostic candidate", "wrong record")
    try expect(try Data(contentsOf: resolved.runtimeURL.appendingPathComponent("Data/weights.bin")) == before, "copy mismatch")
    try expect(try Data(contentsOf: original.runtimeURL.appendingPathComponent("Data/weights.bin")) == before, "original changed")
    let records = try await store.list()
    try expect(records.count == 2 && records.contains(recordsBefore[0]), "original registry record changed")
    try expect(!FileManager.default.contentsOfDirectory(atPath: fixture.models.path).contains(where: { $0.hasPrefix(".import-") }), "staging left behind")
  }

  private static func testInventory() async throws {
    for kind in ["missing", "extra", "directory", "hash", "size"] {
      let fixture = try Fixture(); defer { fixture.cleanup() }
      switch kind {
      case "missing": try FileManager.default.removeItem(at: fixture.source.appendingPathComponent("tokenizer.json"))
      case "extra": try Data("unexpected".utf8).write(to: fixture.source.appendingPathComponent("notes.txt"))
      case "directory": try FileManager.default.createDirectory(at: fixture.source.appendingPathComponent("extra"), withIntermediateDirectories: false)
      case "hash":
        let file = try FileHandle(forWritingTo: fixture.weights); try file.seekToEnd(); try file.seek(toOffset: 4 * 1_024 * 1_024 + 36)
        try file.write(contentsOf: Data([0x52])); try file.close()
      default: try Data("smaller".utf8).write(to: fixture.weights)
      }
      do {
        _ = try await fixture.store.importCoreMLDiagnosticCandidate(manifest: fixture.manifest)
        throw TestFailure(message: "invalid \(kind) inventory accepted")
      } catch is CoreMLDiagnosticCandidate.Failure { }
      let records = try await fixture.store.list()
      try expect(records.isEmpty, "rejected inventory changed registry")
    }
  }

  private static func testSymlinks() async throws {
    for kind in ["root", "parent", "leaf"] {
      let fixture = try Fixture(); defer { fixture.cleanup() }
      let target: URL
      switch kind {
      case "root": target = fixture.source
      case "parent": target = fixture.weights.deletingLastPathComponent()
      default: target = fixture.weights
      }
      let outside = fixture.root.appendingPathComponent("Outside")
      try FileManager.default.moveItem(at: target, to: outside)
      try FileManager.default.createSymbolicLink(at: target, withDestinationURL: outside)
      do {
        _ = try CoreMLDiagnosticCandidate.verify(documentsURL: fixture.documents, manifest: fixture.manifest)
        throw TestFailure(message: "\(kind) symlink accepted")
      } catch CoreMLDiagnosticCandidate.Failure.invalidInventory { }
    }
  }

  private static func testMutation() async throws {
    let fixture = try Fixture(); defer { fixture.cleanup() }
    let store = fixture.store
    let original = try await store.importModel(runtime: .coreML, uri: fixture.source.absoluteString, displayName: "Original")
    let recordsBefore = try await store.list()
    let weights = fixture.weights
    do {
      _ = try await store.importCoreMLDiagnosticCandidate(manifest: fixture.manifest, afterVerification: {
        // Same byte count, but a different hash. The copy's own source identities
        // all match because this occurs before the import plan is constructed.
        try! Data(repeating: 0x52, count: 4 * 1_024 * 1_024 + 37).write(to: weights)
      })
      throw TestFailure(message: "post-verification mutation accepted")
    } catch LocalInferenceError.sourceChangedDuringImport { }
    let records = try await store.list()
    try expect(records == recordsBefore, "mutation changed existing index")
    try expect(try FileManager.default.contentsOfDirectory(atPath: fixture.models.path) == [original.modelId], "mutation left staging or unindexed model")
  }

  private static func testCancellation() async throws {
    let fixture = try Fixture(); defer { fixture.cleanup() }
    let task = Task {
      try await fixture.store.importCoreMLDiagnosticCandidate(manifest: fixture.manifest, afterVerification: {
        withUnsafeCurrentTask { $0?.cancel() }
      })
    }
    do { _ = try await task.value; throw TestFailure(message: "cancelled import succeeded") }
    catch is CancellationError { }
    try expect(try await fixture.store.list().isEmpty, "cancelled import changed registry")
  }

  private static func testCapability() async throws {
    try expect(!CoreMLDiagnosticCandidate.isAvailable, "test process must not expose bundle capability")
    do { _ = try CoreMLDiagnosticCandidate.bundledManifest(); throw TestFailure(message: "missing manifest accepted") }
    catch CoreMLDiagnosticCandidate.Failure.unavailable { }
  }
}
