import Darwin
import Foundation

private struct TestFailure: Error, CustomStringConvertible {
  let description: String

  init(_ description: String) {
    self.description = description
  }
}

private struct TestWorkspace {
  let root: URL
  let source: URL
  let applicationSupport: URL

  init(name: String) throws {
    root = FileManager.default.temporaryDirectory
      .appendingPathComponent("swarmer-store-\(name)-\(UUID().uuidString)", isDirectory: true)
    source = root.appendingPathComponent("Source", isDirectory: true)
    applicationSupport = root.appendingPathComponent("ApplicationSupport", isDirectory: true)
    try FileManager.default.createDirectory(
      at: source,
      withIntermediateDirectories: true
    )
    try FileManager.default.createDirectory(
      at: applicationSupport,
      withIntermediateDirectories: true
    )
  }

  func remove() {
    try? FileManager.default.removeItem(at: root)
  }

  var models: URL {
    applicationSupport
      .appendingPathComponent("SwarmerLocalInference", isDirectory: true)
      .appendingPathComponent("Models", isDirectory: true)
  }
}

@main
private struct LocalModelStoreTests {
  private typealias Test = @Sendable () async throws -> Void

  static func main() async {
    let tests: [(String, Test)] = [
      ("GGUF import and metadata resolution", testGGUFImportAndResolution),
      ("Core ML filtering and required sidecars", testCoreMLFilteringAndSidecars),
      ("MLX direct artifact filtering", testMLXFiltering),
      ("nested symbolic-link rejection", testSymbolicLinkRejection),
      ("staging and orphan cleanup", testRecoveryCleanup),
      ("cancelled import cleanup", testCancellationCleanup),
    ]

    var failures: [String] = []
    for (name, test) in tests {
      do {
        try await test()
        print("PASS: \(name)")
      } catch {
        let failure = "FAIL: \(name): \(error)"
        failures.append(failure)
        print(failure)
      }
    }

    guard failures.isEmpty else {
      print("LocalModelStore: \(failures.count)/\(tests.count) tests failed")
      Darwin.exit(EXIT_FAILURE)
    }
    print("LocalModelStore: \(tests.count)/\(tests.count) tests passed")
  }

  private static func testGGUFImportAndResolution() async throws {
    let workspace = try TestWorkspace(name: "gguf")
    defer { workspace.remove() }
    let bytes = Data([0x47, 0x47, 0x55, 0x46, 0x01, 0x02, 0x03])
    let source = workspace.source.appendingPathComponent("tiny.gguf", isDirectory: false)
    try bytes.write(to: source)

    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    let record = try await store.importModel(
      runtime: .llamaCpp,
      uri: source.absoluteString,
      displayName: "Tiny GGUF"
    )

    try expect(record.runtime == .llamaCpp, "GGUF runtime metadata changed")
    try expect(record.displayName == "Tiny GGUF", "explicit display name was not persisted")
    try expect(record.source == "tiny.gguf", "source filename was not persisted")
    try expect(record.sizeBytes == Int64(bytes.count), "GGUF size metadata is inaccurate")
    try expect(record.runtimeRelativePath == "payload/tiny.gguf", "GGUF path is not confined to payload")
    try expect(record.tokenizerRelativePath == nil, "GGUF unexpectedly has tokenizer metadata")

    let listed = try await store.list()
    try expect(listed.count == 1, "persisted GGUF metadata did not round-trip")
    let persisted = listed[0]
    try expect(persisted.modelId == record.modelId, "persisted GGUF id changed")
    try expect(persisted.runtime == record.runtime, "persisted GGUF runtime changed")
    try expect(persisted.displayName == record.displayName, "persisted GGUF display name changed")
    try expect(persisted.source == record.source, "persisted GGUF source changed")
    try expect(persisted.sizeBytes == record.sizeBytes, "persisted GGUF size changed")
    try expect(persisted.runtimeRelativePath == record.runtimeRelativePath, "persisted GGUF path changed")
    try expect(persisted.tokenizerRelativePath == record.tokenizerRelativePath, "persisted tokenizer path changed")
    try expect(
      abs(persisted.importedAt.timeIntervalSince(record.importedAt)) < 1,
      "persisted GGUF import time changed by one second or more"
    )
    let resolved = try await store.resolve(modelId: record.modelId)
    try expect(resolved.stored == persisted, "resolved GGUF metadata differs from the index")
    try expect(resolved.tokenizerURL == nil, "resolved GGUF unexpectedly has a tokenizer")
    try expect(try Data(contentsOf: resolved.runtimeURL) == bytes, "resolved GGUF bytes differ from source")
    try expect(isWithin(resolved.runtimeURL, root: workspace.models), "resolved GGUF escaped private storage")
  }

  private static func testCoreMLFilteringAndSidecars() async throws {
    let workspace = try TestWorkspace(name: "coreml")
    defer { workspace.remove() }
    let source = workspace.source.appendingPathComponent("CoreModel", isDirectory: true)
    let package = source.appendingPathComponent("Language.mlpackage", isDirectory: true)
    let packageData = package.appendingPathComponent("Data", isDirectory: true)
    try FileManager.default.createDirectory(at: packageData, withIntermediateDirectories: true)
    try write("compiled model", to: packageData.appendingPathComponent("model.mlmodel"))
    try write("{}", to: source.appendingPathComponent("tokenizer.json"))
    try write("{}", to: source.appendingPathComponent("tokenizer_config.json"))
    try write("merge rules", to: source.appendingPathComponent("merges.txt"))
    try write("must not be imported", to: source.appendingPathComponent("notes.md"))
    let unrelated = source.appendingPathComponent("Unrelated", isDirectory: true)
    try FileManager.default.createDirectory(at: unrelated, withIntermediateDirectories: true)
    try write("must not be imported", to: unrelated.appendingPathComponent("private.bin"))

    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    let record = try await store.importModel(
      runtime: .coreML,
      uri: source.absoluteString,
      displayName: nil
    )
    let resolved = try await store.resolve(modelId: record.modelId)
    let importedRoot = try required(resolved.tokenizerURL, "Core ML tokenizer directory is missing")

    try expect(record.displayName == "CoreModel", "Core ML fallback display name changed")
    try expect(resolved.runtimeURL.lastPathComponent == "Language.mlpackage", "wrong Core ML model resolved")
    try expect(fileExists(importedRoot.appendingPathComponent("tokenizer.json")), "tokenizer.json was not copied")
    try expect(fileExists(importedRoot.appendingPathComponent("tokenizer_config.json")), "tokenizer_config.json was not copied")
    try expect(fileExists(importedRoot.appendingPathComponent("merges.txt")), "allowed tokenizer sidecar was not copied")
    try expect(!fileExists(importedRoot.appendingPathComponent("notes.md")), "unselected Core ML file was copied")
    try expect(!fileExists(importedRoot.appendingPathComponent("Unrelated")), "unselected Core ML directory was copied")
    try expect(
      fileExists(resolved.runtimeURL.appendingPathComponent("Data/model.mlmodel")),
      "selected Core ML package contents were not copied"
    )

    let missingSidecar = workspace.source.appendingPathComponent("MissingSidecar", isDirectory: true)
    try FileManager.default.createDirectory(at: missingSidecar, withIntermediateDirectories: true)
    try write("model", to: missingSidecar.appendingPathComponent("Language.mlmodel"))
    try write("{}", to: missingSidecar.appendingPathComponent("tokenizer.json"))
    do {
      _ = try await store.importModel(
        runtime: .coreML,
        uri: missingSidecar.absoluteString,
        displayName: nil
      )
      throw TestFailure("Core ML import accepted a missing tokenizer_config.json")
    } catch LocalInferenceError.unsupportedModel {
      // Expected.
    } catch let failure as TestFailure {
      throw failure
    } catch {
      throw TestFailure("missing Core ML sidecar returned the wrong error: \(error)")
    }
    let recordsAfterRejection = try await store.list()
    try expect(
      recordsAfterRejection.count == 1 && recordsAfterRejection[0].modelId == record.modelId,
      "rejected Core ML import changed the index"
    )
  }

  private static func testMLXFiltering() async throws {
    let workspace = try TestWorkspace(name: "mlx")
    defer { workspace.remove() }
    let source = workspace.source.appendingPathComponent("MLXModel", isDirectory: true)
    try FileManager.default.createDirectory(at: source, withIntermediateDirectories: true)
    try write("{}", to: source.appendingPathComponent("config.json"))
    try write("{}", to: source.appendingPathComponent("tokenizer.json"))
    try write("weights", to: source.appendingPathComponent("model.safetensors"))
    try write("template", to: source.appendingPathComponent("chat_template.jinja"))
    try write("must not be imported", to: source.appendingPathComponent("loader.py"))
    try write("must not be imported", to: source.appendingPathComponent("pytorch_model.bin"))
    let nested = source.appendingPathComponent("Nested", isDirectory: true)
    try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
    try write("must not be imported", to: nested.appendingPathComponent("extra.safetensors"))

    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    let record = try await store.importModel(
      runtime: .mlx,
      uri: source.absoluteString,
      displayName: "MLX Direct"
    )
    let resolved = try await store.resolve(modelId: record.modelId)

    try expect(resolved.runtimeURL == resolved.tokenizerURL, "MLX model and tokenizer roots differ")
    try expect(fileExists(resolved.runtimeURL.appendingPathComponent("config.json")), "MLX config was not copied")
    try expect(fileExists(resolved.runtimeURL.appendingPathComponent("tokenizer.json")), "MLX tokenizer was not copied")
    try expect(fileExists(resolved.runtimeURL.appendingPathComponent("model.safetensors")), "MLX weights were not copied")
    try expect(fileExists(resolved.runtimeURL.appendingPathComponent("chat_template.jinja")), "allowed MLX template was not copied")
    try expect(!fileExists(resolved.runtimeURL.appendingPathComponent("loader.py")), "MLX Python code was copied")
    try expect(!fileExists(resolved.runtimeURL.appendingPathComponent("pytorch_model.bin")), "unselected MLX weights were copied")
    try expect(!fileExists(resolved.runtimeURL.appendingPathComponent("Nested")), "nested MLX artifacts were copied")
  }

  private static func testSymbolicLinkRejection() async throws {
    let workspace = try TestWorkspace(name: "symlink")
    defer { workspace.remove() }
    let source = workspace.source.appendingPathComponent("CoreModel", isDirectory: true)
    let package = source.appendingPathComponent("Language.mlpackage", isDirectory: true)
    try FileManager.default.createDirectory(at: package, withIntermediateDirectories: true)
    try write("{}", to: source.appendingPathComponent("tokenizer.json"))
    try write("{}", to: source.appendingPathComponent("tokenizer_config.json"))
    let external = workspace.source.appendingPathComponent("external.bin")
    try write("outside", to: external)
    try FileManager.default.createSymbolicLink(
      at: package.appendingPathComponent("linked.bin"),
      withDestinationURL: external
    )

    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    do {
      _ = try await store.importModel(
        runtime: .coreML,
        uri: source.absoluteString,
        displayName: nil
      )
      throw TestFailure("Core ML import followed a nested symbolic link")
    } catch LocalInferenceError.symbolicLinkRejected {
      // Expected.
    }
    let recordsAfterRejection = try await store.list()
    try expect(recordsAfterRejection.isEmpty, "symbolic-link rejection left indexed metadata")
    try expect(try ownedModelDirectories(in: workspace.models).isEmpty, "symbolic-link rejection left model data")
  }

  private static func testRecoveryCleanup() async throws {
    let workspace = try TestWorkspace(name: "recovery")
    defer { workspace.remove() }
    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    _ = try await store.list()

    let staging = workspace.models.appendingPathComponent(
      ".import-\(UUID().uuidString.lowercased())",
      isDirectory: true
    )
    let orphan = workspace.models.appendingPathComponent(
      UUID().uuidString.lowercased(),
      isDirectory: true
    )
    let foreign = workspace.models.appendingPathComponent("operator-notes", isDirectory: true)
    for directory in [staging, orphan, foreign] {
      try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
      try write("partial", to: directory.appendingPathComponent("partial.bin"))
    }

    let recoveredRecords = try await store.list()
    try expect(recoveredRecords.isEmpty, "recovery cleanup invented model metadata")
    try expect(!fileExists(staging), "stale staging directory survived recovery")
    try expect(!fileExists(orphan), "unindexed canonical model directory survived recovery")
    try expect(fileExists(foreign), "recovery cleanup removed a non-owned directory")
  }

  private static func testCancellationCleanup() async throws {
    let workspace = try TestWorkspace(name: "cancellation")
    defer { workspace.remove() }
    let source = workspace.source.appendingPathComponent("large.gguf", isDirectory: false)
    guard FileManager.default.createFile(atPath: source.path, contents: nil) else {
      throw TestFailure("could not create cancellation fixture")
    }
    let handle = try FileHandle(forWritingTo: source)
    try handle.truncate(atOffset: 256 * 1_024 * 1_024)
    try handle.close()

    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    let importTask = Task {
      try await store.importModel(
        runtime: .llamaCpp,
        uri: source.absoluteString,
        displayName: "Cancelled"
      )
    }

    var observedStaging = false
    for _ in 0..<2_000 {
      let names = (try? FileManager.default.contentsOfDirectory(atPath: workspace.models.path)) ?? []
      if names.contains(where: { $0.hasPrefix(".import-") }) {
        observedStaging = true
        importTask.cancel()
        break
      }
      try await Task.sleep(for: .milliseconds(1))
    }
    guard observedStaging else {
      importTask.cancel()
      _ = try? await importTask.value
      throw TestFailure("could not observe an in-progress staged import")
    }

    do {
      _ = try await importTask.value
      throw TestFailure("cancelled import completed successfully")
    } catch is CancellationError {
      // Expected.
    }
    let recordsAfterCancellation = try await store.list()
    try expect(recordsAfterCancellation.isEmpty, "cancelled import changed the index")
    try expect(try ownedModelDirectories(in: workspace.models).isEmpty, "cancelled import left owned model data")
  }

  private static func expect(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
    guard try condition() else { throw TestFailure(message) }
  }

  private static func required<T>(_ value: T?, _ message: String) throws -> T {
    guard let value else { throw TestFailure(message) }
    return value
  }

  private static func write(_ value: String, to url: URL) throws {
    try Data(value.utf8).write(to: url)
  }

  private static func fileExists(_ url: URL) -> Bool {
    FileManager.default.fileExists(atPath: url.path)
  }

  private static func isWithin(_ value: URL, root: URL) -> Bool {
    value.standardizedFileURL.path.hasPrefix(root.standardizedFileURL.path + "/")
  }

  private static func ownedModelDirectories(in models: URL) throws -> [String] {
    guard fileExists(models) else { return [] }
    return try FileManager.default.contentsOfDirectory(atPath: models.path).filter { name in
      if name.hasPrefix(".import-") { return true }
      guard let uuid = UUID(uuidString: name) else { return false }
      return uuid.uuidString.lowercased() == name
    }
  }
}
