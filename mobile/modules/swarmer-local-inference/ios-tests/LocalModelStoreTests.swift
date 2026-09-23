import Darwin
import Foundation
import CryptoKit

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
  let documents: URL

  init(name: String) throws {
    root = FileManager.default.temporaryDirectory
      .appendingPathComponent("swarmer-store-\(name)-\(UUID().uuidString)", isDirectory: true)
    source = root.appendingPathComponent("Source", isDirectory: true)
    applicationSupport = root.appendingPathComponent("ApplicationSupport", isDirectory: true)
    documents = root.appendingPathComponent("Documents", isDirectory: true)
    try FileManager.default.createDirectory(
      at: source,
      withIntermediateDirectories: true
    )
    try FileManager.default.createDirectory(
      at: applicationSupport,
      withIntermediateDirectories: true
    )
    try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
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

private actor ImportCheckpointGate {
  private var reached = false
  private var released = false
  private var reachedWaiters: [CheckedContinuation<Void, Never>] = []
  private var releaseWaiter: CheckedContinuation<Void, Never>?

  func pauseAfterStagingCreation() async {
    reached = true
    for waiter in reachedWaiters {
      waiter.resume()
    }
    reachedWaiters.removeAll()

    guard !released else { return }
    await withCheckedContinuation { continuation in
      releaseWaiter = continuation
    }
  }

  func waitUntilReached() async {
    guard !reached else { return }
    await withCheckedContinuation { continuation in
      reachedWaiters.append(continuation)
    }
  }

  func release() {
    released = true
    releaseWaiter?.resume()
    releaseWaiter = nil
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
      ("pinned E5 uses a validated BERT encoder and cannot generate language", testE5Profile),
      ("legacy durable E5 keeps its identity and files while gaining embedding purpose", testLegacyE5Purpose),
      ("legacy imported encoder purpose is derived from its existing configuration", testLegacyImportedEncoderPurpose),
      ("cached MLX becomes independent durable files and keeps pinned provenance", testDurableMLXCachePromotion),
      ("durable MLX preserves multi-chunk hashes and detects final-byte corruption", testDurableMLXMultiChunkIntegrity),
      ("durable MLX copy and verification retain less than 64 MiB per phase", testDurableMLXMemoryBudget),
      ("durable MLX rejects changed files and manifest", testDurableMLXIntegrity),
      ("MLX cache confinement and complete shard index", testDurableMLXRejectsUnsafeCache),
      ("durable MLX cancellation preserves Files additions and active staging", testDurableMLXCancellation),
      ("durable MLX recovers its manifest without deleting unregistered Files folders", testDurableMLXRecovery),
      ("an unregistered Documents manifest cannot claim a pinned Hub revision", testDurableMLXRejectsUntrustedManifest),
      ("nested symbolic-link rejection", testSymbolicLinkRejection),
      ("staging and orphan cleanup", testRecoveryCleanup),
      ("cancelled import cleanup", testCancellationCleanup),
      ("pinned GGUF download metadata validation", testDownloadMetadataValidation),
      ("download size and SHA-256 verification", testDownloadVerification),
      ("cancelled download exits before network or import", testDownloadCancellation),
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

  private static func download(
    repoId: String = "example/Dolphin-3B-GGUF",
    revision: String = String(repeating: "a", count: 40),
    filename: String = "Dolphin-Q4_K_M.gguf",
    sha256: String = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    sizeBytes: Int64 = 3,
    displayName: String = "Dolphin 3B"
  ) throws -> LocalModelDownload {
    try LocalModelDownload(
      repoId: repoId,
      revision: revision,
      filename: filename,
      sha256: sha256,
      sizeBytes: sizeBytes,
      displayName: displayName
    )
  }

  private static func testDownloadMetadataValidation() async throws {
    let descriptor = try download(revision: String(repeating: "A", count: 40))
    try expect(
      descriptor.url.absoluteString == "https://huggingface.co/example/Dolphin-3B-GGUF/resolve/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/Dolphin-Q4_K_M.gguf",
      "download URL was not pinned to an immutable HTTPS file"
    )
    let invalidMetadata: [() throws -> LocalModelDownload] = [
      { try download(repoId: "https://example.org/model") },
      { try download(repoId: "../escape") },
      { try download(revision: "main") },
      { try download(filename: "../escape.gguf") },
      { try download(filename: "nested/model.gguf") },
      { try download(filename: "model.gguf?download=1") },
      { try download(filename: "model.safetensors") },
      { try download(sha256: String(repeating: "x", count: 64)) },
      { try download(sha256: String(repeating: "a", count: 63)) },
    ]
    for make in invalidMetadata {
      do {
        _ = try make()
        throw TestFailure("unsafe download metadata was accepted")
      } catch LocalInferenceError.invalidDownloadMetadata {
        // Expected.
      }
    }
    for size in [Int64(0), Int64(-1), LocalModelDownload.maximumBytes + 1] {
      do {
        _ = try download(sizeBytes: size)
        throw TestFailure("download accepted an invalid byte budget")
      } catch LocalInferenceError.importTooLarge {
        // Expected.
      }
    }
  }

  private static func testDownloadVerification() async throws {
    let workspace = try TestWorkspace(name: "download-verification")
    defer { workspace.remove() }
    let file = workspace.source.appendingPathComponent("Dolphin-Q4_K_M.gguf")
    let descriptor = try download()
    // SHA-256("abc") is the standard independently known test vector.
    try write("abc", to: file)
    try descriptor.verifyDownloadedFile(at: file)
    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    let imported = try await store.importModel(
      runtime: .llamaCpp, uri: file.absoluteString, displayName: descriptor.displayName
    )
    try expect(imported.sizeBytes == 3, "verified file could not be imported into private storage")

    try write("abd", to: file)
    do {
      try descriptor.verifyDownloadedFile(at: file)
      throw TestFailure("same-size tampering passed checksum verification")
    } catch LocalInferenceError.downloadChecksumMismatch {
      // Expected.
    }
    try write("ab", to: file)
    do {
      try descriptor.verifyDownloadedFile(at: file)
      throw TestFailure("truncated download passed size verification")
    } catch LocalInferenceError.downloadSizeMismatch {
      // Expected.
    }
    try write("abcd", to: file)
    do {
      try descriptor.verifyDownloadedFile(at: file)
      throw TestFailure("oversized download passed size verification")
    } catch LocalInferenceError.downloadSizeMismatch {
      // Expected.
    }
  }

  private static func testDownloadCancellation() async throws {
    let workspace = try TestWorkspace(name: "download-cancel")
    defer { workspace.remove() }
    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    let gate = ImportCheckpointGate()
    let descriptor = try download()
    let task = Task {
      await gate.pauseAfterStagingCreation()
      return try await descriptor.downloadAndImport(into: store)
    }
    await gate.waitUntilReached()
    task.cancel()
    await gate.release()
    do {
      _ = try await task.value
      throw TestFailure("cancelled download did not throw cancellation")
    } catch is CancellationError {
      // Reaching this without a network request also proves preflight cancellation.
    }
    let records = try await store.list()
    try expect(records.isEmpty, "cancelled download modified the model index")
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
    try write("{\"eos_token_id\":[128001,128008,128009]}", to: source.appendingPathComponent("generation_config.json"))
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
    try expect(fileExists(importedRoot.appendingPathComponent("generation_config.json")), "generation_config.json stop-token metadata was not copied")
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
    try Data([0x47, 0x47, 0x55, 0x46]).write(to: source)

    let checkpoint = ImportCheckpointGate()
    let store = LocalModelStore(
      applicationSupportURL: workspace.applicationSupport,
      importDidCreateStaging: {
        await checkpoint.pauseAfterStagingCreation()
      }
    )
    let importTask = Task {
      try await store.importModel(
        runtime: .llamaCpp,
        uri: source.absoluteString,
        displayName: "Cancelled"
      )
    }

    await checkpoint.waitUntilReached()
    let stagedModels = try ownedModelDirectories(in: workspace.models)
    try expect(
      stagedModels.count == 1 && stagedModels[0].hasPrefix(".import-"),
      "import checkpoint was reached without owned staging data"
    )
    importTask.cancel()
    await checkpoint.release()

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

  private static let repositoryId = "example/Dolphin-3B-4bit"
  private static let revision = String(repeating: "c", count: 40)

  private static func e5Configuration() throws -> [String: Any] {
    // Semantic copy of the pinned upstream config, not a guessed architecture:
    // intfloat/multilingual-e5-small@614241f622f53c4eeff9890bdc4f31cfecc418b3/config.json
    let fixture = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
      .appendingPathComponent("fixtures/e5-small-config.json")
    return try required(try JSONSerialization.jsonObject(with: Data(contentsOf: fixture)) as? [String: Any], "invalid E5 fixture")
  }

  private static func testE5Profile() async throws {
    let config = try e5Configuration()
    try EmbeddingValidation.validateE5Configuration(config)
    try expect(LocalModelPurpose.mlx(configuration: config) == .embeddings, "E5 is exposed as a generator")
    do {
      try LocalModelPurpose.requireGeneration(configuration: config)
      throw TestFailure("BERT entered the generation runtime")
    } catch LocalInferenceError.unsupportedModel(_) {}
    try LocalModelPurpose.requireGeneration(configuration: ["model_type": "llama"])
    let invalid: [(String, Any)] = [
      ("model_type", "xlm-roberta"), ("model_type", "llama"),
      ("architectures", ["BertForMaskedLM"]), ("hidden_size", 768),
      ("intermediate_size", 3072), ("num_hidden_layers", 6),
      ("num_attention_heads", 6), ("max_position_embeddings", 514),
      ("type_vocab_size", 1), ("vocab_size", 30522), ("tokenizer_class", "BertTokenizer"),
    ]
    for (key, value) in invalid {
      var changed = config
      changed[key] = value
      do {
        try EmbeddingValidation.validateE5Configuration(changed)
        throw TestFailure("E5 accepted incompatible field \(key)")
      } catch EmbeddingValidation.ValidationError.configuration {}
    }
  }

  private static func removePurpose(at url: URL, array: Bool) throws -> Data {
    let object = try JSONSerialization.jsonObject(with: Data(contentsOf: url))
    let legacy: Any
    if array {
      legacy = (object as! [[String: Any]]).map { row -> [String: Any] in
        var changed = row; changed.removeValue(forKey: "purpose"); return changed
      }
    } else {
      var changed = object as! [String: Any]
      changed.removeValue(forKey: "purpose")
      legacy = changed
    }
    let bytes = try JSONSerialization.data(withJSONObject: legacy, options: [.sortedKeys])
    try bytes.write(to: url)
    return bytes
  }

  private static func testLegacyE5Purpose() async throws {
    let workspace = try TestWorkspace(name: "e5-legacy")
    defer { workspace.remove() }
    let cached = try makeCachedMLX(in: workspace)
    try JSONSerialization.data(withJSONObject: e5Configuration()).write(to: cached.cache.appendingPathComponent("blobs/config.json"))
    let store = durableStore(workspace)
    let saved = try await store.preserveMLXSnapshot(at: cached.snapshot, repositoryCacheURL: cached.cache,
      repositoryId: EmbeddingValidation.repository, revision: revision)
    try expect(saved.stored.purpose == .embeddings, "new E5 metadata lost its purpose")
    let index = workspace.applicationSupport.appendingPathComponent("SwarmerLocalInference/models.json")
    let manifest = saved.runtimeURL.deletingLastPathComponent().appendingPathComponent("swarmer-model.json")
    let indexBefore = try removePurpose(at: index, array: true)
    let manifestBefore = try removePurpose(at: manifest, array: false)
    try FileManager.default.removeItem(at: cached.cache)
    let reopened = durableStore(workspace)
    let records = try await reopened.list()
    try expect(records[0].purpose == nil && records[0].effectivePurpose == .embeddings, "legacy immutable origin was not classified")
    let purpose = try await reopened.purpose(for: records[0])
    try expect(purpose == .embeddings, "legacy E5 could enter generation choices")
    let resolved = try required(try await reopened.resolveRemoteMLX(repositoryId: EmbeddingValidation.repository, revision: revision), "legacy E5 no longer resolves offline")
    try expect(resolved.stored.modelId == saved.stored.modelId && resolved.runtimeURL == saved.runtimeURL, "migration changed model identity or location")
    try expect(try Data(contentsOf: index) == indexBefore && Data(contentsOf: manifest) == manifestBefore, "classification rewrote signed provenance")
    try expect(try Data(contentsOf: resolved.runtimeURL.appendingPathComponent("model.safetensors")) == Data("abc".utf8), "weights changed")
  }

  private static func testLegacyImportedEncoderPurpose() async throws {
    let workspace = try TestWorkspace(name: "encoder-legacy")
    defer { workspace.remove() }
    let source = workspace.source.appendingPathComponent("encoder")
    try FileManager.default.createDirectory(at: source, withIntermediateDirectories: true)
    try JSONSerialization.data(withJSONObject: e5Configuration()).write(to: source.appendingPathComponent("config.json"))
    try write("{}", to: source.appendingPathComponent("tokenizer.json"))
    try write("abc", to: source.appendingPathComponent("model.safetensors"))
    let store = durableStore(workspace)
    let imported = try await store.importModel(runtime: .mlx, uri: source.absoluteString, displayName: nil)
    try expect(imported.purpose == .embeddings, "new local encoder purpose missing")
    let index = workspace.applicationSupport.appendingPathComponent("SwarmerLocalInference/models.json")
    let before = try removePurpose(at: index, array: true)
    let reopened = durableStore(workspace)
    let legacy = try await reopened.list()[0]
    let purpose = try await reopened.purpose(for: legacy)
    try expect(purpose == .embeddings, "legacy imported encoder not recognized")
    try expect(try Data(contentsOf: index) == before, "legacy import classification rewrote the registry")
  }

  private static func makeCachedMLX(in workspace: TestWorkspace) throws -> (cache: URL, snapshot: URL) {
    let cache = workspace.source.appendingPathComponent("models--example--Dolphin-3B-4bit")
    let snapshot = cache.appendingPathComponent("snapshots/\(revision)")
    let blobs = cache.appendingPathComponent("blobs")
    try FileManager.default.createDirectory(at: snapshot, withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: blobs, withIntermediateDirectories: true)
    let files = [
      "config.json": "{\"model_type\":\"llama\"}",
      "tokenizer.json": "{\"model\":{}}",
      "tokenizer_config.json": "{\"chat_template\":\"{{ messages }}\"}",
      "model.safetensors": "abc",
      "model.safetensors.index.json": "{\"weight_map\":{\"weight\":\"model.safetensors\"}}",
      "chat_template.jinja": "{{ messages }}",
    ]
    for (name, contents) in files {
      try write(contents, to: blobs.appendingPathComponent(name))
      try FileManager.default.createSymbolicLink(
        atPath: snapshot.appendingPathComponent(name).path,
        withDestinationPath: "../../blobs/\(name)"
      )
    }
    return (cache, snapshot)
  }

  private static func durableStore(_ workspace: TestWorkspace) -> LocalModelStore {
    LocalModelStore(applicationSupportURL: workspace.applicationSupport, documentsURL: workspace.documents)
  }

  private static func testDurableMLXCachePromotion() async throws {
    let workspace = try TestWorkspace(name: "mlx-durable")
    defer { workspace.remove() }
    let cached = try makeCachedMLX(in: workspace)
    let store = durableStore(workspace)
    let result = try await store.preserveMLXSnapshot(
      at: cached.snapshot, repositoryCacheURL: cached.cache, repositoryId: repositoryId, revision: revision
    )
    try expect(isWithin(result.runtimeURL, root: workspace.documents.appendingPathComponent("Models")), "MLX was not stored in Documents/Models")
    try expect(result.stored.source.hasPrefix("Documents/Models"), "durable source is not exposed to the model list")
    let origin = try required(result.stored.remoteOrigin, "immutable origin was lost")
    try expect(origin.repositoryId == repositoryId && origin.revision == revision, "pinned repo/revision changed")
    let weight = try required(origin.files.first(where: { $0.filename == "model.safetensors" }), "weights are not in the manifest")
    try expect(weight.sha256 == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "copy did not hash the actual weight bytes")
    for file in origin.files {
      let destination = result.runtimeURL.appendingPathComponent(file.filename)
      let values = try destination.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
      try expect(values.isRegularFile == true && values.isSymbolicLink != true, "durable artifact still depends on a cache symlink")
      try expect(try Data(contentsOf: destination) == Data(contentsOf: cached.snapshot.appendingPathComponent(file.filename)), "materialized bytes differ from the cache")
    }
    try FileManager.default.removeItem(at: cached.cache)
    let reopened = durableStore(workspace)
    let records = try await reopened.list()
    try expect(records.count == 1 && records[0].modelId == result.stored.modelId, "local persistent entry did not survive reopening")
    let loaded = try required(
      try await reopened.resolveRemoteMLX(repositoryId: repositoryId, revision: revision),
      "deleting the entire Hub cache lost the pinned model"
    )
    try expect(try String(contentsOf: loaded.runtimeURL.appendingPathComponent("model.safetensors"), encoding: .utf8) == "abc", "durable model no longer resolves independently")
    let repeated = try await reopened.preserveMLXSnapshot(
      at: cached.snapshot, repositoryCacheURL: cached.cache, repositoryId: repositoryId, revision: revision
    )
    try expect(repeated.stored.modelId == result.stored.modelId, "repeat load made another copy or required the deleted cache")
    let local = try await reopened.resolve(modelId: result.stored.modelId)
    try expect(local.runtimeURL == loaded.runtimeURL, "local UUID and pinned remote id resolve to different files")
    let wrongRevision = try await reopened.resolveRemoteMLX(repositoryId: repositoryId, revision: String(repeating: "d", count: 40))
    try expect(wrongRevision == nil, "a different revision reused the wrong durable model")
  }

  private static func testDurableMLXMultiChunkIntegrity() async throws {
    let workspace = try TestWorkspace(name: "mlx-multi-chunk")
    defer { workspace.remove() }
    let cached = try makeCachedMLX(in: workspace)
    let chunkBytes = 4 * 1_024 * 1_024
    var bytes = Data(repeating: 0x11, count: chunkBytes)
    bytes.append(Data(repeating: 0x7f, count: chunkBytes))
    bytes.append(Data(repeating: 0xd3, count: 137))
    let expectedDigest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
    try bytes.write(to: cached.cache.appendingPathComponent("blobs/model.safetensors"))

    let result = try await durableStore(workspace).preserveMLXSnapshot(
      at: cached.snapshot, repositoryCacheURL: cached.cache, repositoryId: repositoryId, revision: revision
    )
    let origin = try required(result.stored.remoteOrigin, "multi-chunk promotion lost pinned provenance")
    let weight = try required(origin.files.first(where: { $0.filename == "model.safetensors" }), "multi-chunk weights lack a digest")
    try expect(weight.sizeBytes == Int64(2 * chunkBytes + 137), "the partial final chunk changed the recorded size")
    try expect(weight.sha256 == expectedDigest, "streamed copy digest differs from the complete source digest")
    let weights = result.runtimeURL.appendingPathComponent("model.safetensors")
    try expect(try Data(contentsOf: weights) == bytes, "multi-chunk durable copy changed the weights")
    try FileManager.default.removeItem(at: cached.cache)

    let reopened = durableStore(workspace)
    let resolved = try required(
      try await reopened.resolveRemoteMLX(repositoryId: repositoryId, revision: revision),
      "multi-chunk durable model did not resolve independently of its cache"
    )
    try expect(resolved.stored.remoteOrigin == origin, "resolution changed the persisted file digests")
    let local = try await reopened.resolve(modelId: result.stored.modelId)
    try expect(local.runtimeURL == resolved.runtimeURL, "local and pinned multi-chunk resolution disagree")

    let writer = try FileHandle(forWritingTo: weights)
    defer { try? writer.close() }
    try writer.seek(toOffset: UInt64(bytes.count - 1))
    try writer.write(contentsOf: Data([0xd2]))
    try writer.synchronize()
    try writer.close()
    try expect(try weights.resourceValues(forKeys: [.fileSizeKey]).fileSize == bytes.count, "corruption fixture changed the file size")
    do {
      _ = try await reopened.resolveRemoteMLX(repositoryId: repositoryId, revision: revision)
      throw TestFailure("last-byte corruption beyond two complete chunks retained pinned provenance")
    } catch LocalInferenceError.sourceChangedDuringImport { }
    do {
      _ = try await reopened.resolve(modelId: result.stored.modelId)
      throw TestFailure("local resolution accepted the corrupted partial final chunk")
    } catch LocalInferenceError.sourceChangedDuringImport { }
  }

  private static func physicalFootprint() throws -> UInt64 {
    var information = task_vm_info_data_t()
    var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<integer_t>.size)
    let capacity = Int(count)
    let status = withUnsafeMutablePointer(to: &information) { pointer in
      pointer.withMemoryRebound(to: integer_t.self, capacity: capacity) {
        task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
      }
    }
    try expect(status == KERN_SUCCESS, "Mach could not measure the process physical footprint")
    return information.phys_footprint
  }

  private static func testDurableMLXMemoryBudget() async throws {
    let workspace = try TestWorkspace(name: "mlx-memory-budget")
    defer { workspace.remove() }
    let cached = try makeCachedMLX(in: workspace)
    let writer = try FileHandle(forWritingTo: cached.cache.appendingPathComponent("blobs/model.safetensors"))
    defer { try? writer.close() }
    try writer.truncate(atOffset: 0)
    let chunk = Data(repeating: 0x5b, count: 4 * 1_024 * 1_024)
    for _ in 0..<32 { try writer.write(contentsOf: chunk) }
    try writer.synchronize()
    try writer.close()
    try await measureDurableMLXPhases(store: durableStore(workspace), cache: cached.cache, snapshot: cached.snapshot)
  }

  // Measure on the store's executor, before returning through an actor hop that could
  // drain Foundation temporaries. Exercise production methods, not a duplicate loop.
  private static func measureDurableMLXPhases(
    store: isolated LocalModelStore, cache: URL, snapshot: URL
  ) async throws {
    let beforeCopy = try physicalFootprint()
    let preserved = try await store.preserveMLXSnapshot(
      at: snapshot, repositoryCacheURL: cache, repositoryId: repositoryId, revision: revision
    )
    let afterCopy = try physicalFootprint()
    let resolved = try store.resolve(modelId: preserved.stored.modelId)
    let afterResolve = try physicalFootprint()
    try expect(resolved.stored.modelId == preserved.stored.modelId, "memory measurement resolved another model")
    let copiedWeight = try required(
      preserved.stored.remoteOrigin?.files.first(where: { $0.filename == "model.safetensors" }),
      "memory measurement did not preserve the weight artifact"
    )
    try expect(copiedWeight.sizeBytes == 128 * 1_024 * 1_024, "memory measurement did not exercise a 128 MiB file")
    let copyGrowth = afterCopy > beforeCopy ? afterCopy - beforeCopy : 0
    let resolveGrowth = afterResolve > afterCopy ? afterResolve - afterCopy : 0
    print("MLX physical-footprint growth: preserve=\(copyGrowth) bytes, resolve=\(resolveGrowth) bytes")
    let budget: UInt64 = 64 * 1_024 * 1_024
    try expect(copyGrowth <= budget && resolveGrowth <= budget,
      "durable MLX retained more than 64 MiB: preserve=\(copyGrowth), resolve=\(resolveGrowth)")
  }

  private static func testDurableMLXIntegrity() async throws {
    let workspace = try TestWorkspace(name: "mlx-integrity")
    defer { workspace.remove() }
    let cached = try makeCachedMLX(in: workspace)
    let store = durableStore(workspace)
    let result = try await store.preserveMLXSnapshot(
      at: cached.snapshot, repositoryCacheURL: cached.cache, repositoryId: repositoryId, revision: revision
    )
    let weights = result.runtimeURL.appendingPathComponent("model.safetensors")
    try write("abd", to: weights)
    // Listing remains metadata-only even if a user changes a large file through Files.
    let listed = try await store.list()
    try expect(listed.count == 1, "model listing attempted to hash mutable model files")
    do {
      _ = try await store.resolveRemoteMLX(repositoryId: repositoryId, revision: revision)
      throw TestFailure("same-size changes retained trusted pinned provenance")
    } catch LocalInferenceError.sourceChangedDuringImport { }
    try write("abc", to: weights)
    let manifest = result.runtimeURL.deletingLastPathComponent().appendingPathComponent("swarmer-model.json")
    let original = try String(contentsOf: manifest, encoding: .utf8)
    try write(original.replacingOccurrences(of: revision, with: String(repeating: "d", count: 40)), to: manifest)
    do {
      _ = try await store.resolve(modelId: result.stored.modelId)
      throw TestFailure("a Files manifest edit changed privately registered provenance")
    } catch LocalInferenceError.metadataCorrupt { }
  }

  private static func testDurableMLXRejectsUnsafeCache() async throws {
    let workspace = try TestWorkspace(name: "mlx-unsafe")
    defer { workspace.remove() }
    let cached = try makeCachedMLX(in: workspace)
    let store = durableStore(workspace)
    let weights = cached.snapshot.appendingPathComponent("model.safetensors")
    try FileManager.default.removeItem(at: weights)
    let foreign = workspace.source.appendingPathComponent("unrelated-private-file")
    try write("abc", to: foreign)
    try FileManager.default.createSymbolicLink(at: weights, withDestinationURL: foreign)
    do {
      _ = try await store.preserveMLXSnapshot(at: cached.snapshot, repositoryCacheURL: cached.cache, repositoryId: repositoryId, revision: revision)
      throw TestFailure("a cache symlink escaped the expected repository")
    } catch LocalInferenceError.symbolicLinkRejected { }
    try FileManager.default.removeItem(at: weights)
    try FileManager.default.createSymbolicLink(atPath: weights.path, withDestinationPath: "../../blobs/model.safetensors")
    try write("{\"weight_map\":{\"weight\":\"missing-shard.safetensors\"}}", to: cached.cache.appendingPathComponent("blobs/model.safetensors.index.json"))
    do {
      _ = try await store.preserveMLXSnapshot(at: cached.snapshot, repositoryCacheURL: cached.cache, repositoryId: repositoryId, revision: revision)
      throw TestFailure("an incomplete sharded model was promoted")
    } catch LocalInferenceError.sourceMissing { }
    let records = try await store.list()
    try expect(records.isEmpty, "rejected cache was published in the model registry")
    try expect(!fileExists(workspace.documents.appendingPathComponent("Models")), "rejected model published a partial Documents tree")
    try expect(try ownedModelDirectories(in: workspace.models).isEmpty, "rejected model left private staging")
  }

  private static func testDurableMLXCancellation() async throws {
    let workspace = try TestWorkspace(name: "mlx-cancel")
    defer { workspace.remove() }
    let cached = try makeCachedMLX(in: workspace)
    let foreign = workspace.documents.appendingPathComponent("Models/\(UUID().uuidString.lowercased())/notes.txt")
    try FileManager.default.createDirectory(at: foreign.deletingLastPathComponent(), withIntermediateDirectories: true)
    try write("user model notes", to: foreign)
    let checkpoint = ImportCheckpointGate()
    let store = LocalModelStore(
      applicationSupportURL: workspace.applicationSupport, documentsURL: workspace.documents,
      importDidCreateStaging: { await checkpoint.pauseAfterStagingCreation() }
    )
    let task = Task {
      try await store.preserveMLXSnapshot(at: cached.snapshot, repositoryCacheURL: cached.cache, repositoryId: repositoryId, revision: revision)
    }
    await checkpoint.waitUntilReached()
    let listedDuringCopy = try await store.list()
    try expect(listedDuringCopy.isEmpty, "incomplete promotion was visible in the registry")
    try expect(try ownedModelDirectories(in: workspace.models).count == 1, "listing removed active staging during an await")
    task.cancel()
    await checkpoint.release()
    do {
      _ = try await task.value
      throw TestFailure("cancelled promotion succeeded")
    } catch is CancellationError { }
    try expect(try ownedModelDirectories(in: workspace.models).isEmpty, "cancelled promotion retained staging")
    try expect(try String(contentsOf: foreign, encoding: .utf8) == "user model notes", "Files content was modified by private cleanup")
    try expect(fileExists(cached.snapshot.appendingPathComponent("model.safetensors")), "promotion removed the original cache")
  }

  private static func testDurableMLXRecovery() async throws {
    let workspace = try TestWorkspace(name: "mlx-recovery")
    defer { workspace.remove() }
    let cached = try makeCachedMLX(in: workspace)
    let store = LocalModelStore(
      applicationSupportURL: workspace.applicationSupport, documentsURL: workspace.documents,
      promotionDidPublish: { throw TestFailure("simulated interruption before registry write") }
    )
    do {
      _ = try await store.preserveMLXSnapshot(at: cached.snapshot, repositoryCacheURL: cached.cache, repositoryId: repositoryId, revision: revision)
      throw TestFailure("publication interruption was not exercised")
    } catch let error as TestFailure {
      try expect(error.description == "simulated interruption before registry write", "promotion failed outside the intended crash window")
    }
    let published = workspace.documents.appendingPathComponent("Models/\(repositoryId)/\(revision)/payload")
    let foreign = workspace.documents.appendingPathComponent("Models/\(UUID().uuidString.lowercased())")
    try FileManager.default.createDirectory(at: foreign, withIntermediateDirectories: true)
    let reopened = durableStore(workspace)
    let emptyRegistry = try await reopened.list()
    try expect(emptyRegistry.isEmpty && fileExists(foreign) && fileExists(published), "private cleanup deleted unregistered Documents folders")
    let recovered = try required(try await reopened.resolveRemoteMLX(repositoryId: repositoryId, revision: revision), "complete manifest was not recovered")
    try expect(recovered.runtimeURL.path == published.path, "private pending record recovered different model files")
    let records = try await reopened.list()
    try expect(records.count == 1 && fileExists(foreign), "manifest recovery changed unrelated Files additions")
    let privateFiles = try FileManager.default.contentsOfDirectory(atPath: workspace.applicationSupport.appendingPathComponent("SwarmerLocalInference").path)
    try expect(!privateFiles.contains(where: { $0.hasPrefix(".pending-mlx-") }), "recovery retained a completed pending record")
  }

  private static func testDurableMLXRejectsUntrustedManifest() async throws {
    let workspace = try TestWorkspace(name: "mlx-untrusted-manifest")
    defer { workspace.remove() }
    let cached = try makeCachedMLX(in: workspace)
    let store = durableStore(workspace)
    let result = try await store.preserveMLXSnapshot(at: cached.snapshot, repositoryCacheURL: cached.cache, repositoryId: repositoryId, revision: revision)
    try FileManager.default.removeItem(at: workspace.applicationSupport.appendingPathComponent("SwarmerLocalInference/models.json"))
    let manifestURL = result.runtimeURL.deletingLastPathComponent().appendingPathComponent("swarmer-model.json")
    let modified = Data("abd".utf8)
    try modified.write(to: result.runtimeURL.appendingPathComponent("model.safetensors"))
    let manifest = try String(contentsOf: manifestURL, encoding: .utf8)
    let falseHash = SHA256.hash(data: modified).map { String(format: "%02x", $0) }.joined()
    try write(manifest.replacingOccurrences(
      of: "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", with: falseHash
    ), to: manifestURL)
    do {
      _ = try await durableStore(workspace).resolveRemoteMLX(repositoryId: repositoryId, revision: revision)
      throw TestFailure("a self-consistent user-edited manifest invented immutable Hub provenance")
    } catch LocalInferenceError.unsupportedModel { }
    try expect(fileExists(manifestURL), "untrusted user files were deleted instead of left for explicit import")
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
