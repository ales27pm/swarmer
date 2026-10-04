import CryptoKit
import Darwin
import Foundation

private struct Failure: Error, CustomStringConvertible { let description: String }
private func expect(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
  if try !condition() { throw Failure(description: message) }
}

private actor Gate {
  private var reached = false
  private var released = false
  private var entered: [CheckedContinuation<Void, Never>] = []
  private var waiting: CheckedContinuation<Void, Never>?
  func pause() async {
    reached = true
    for item in entered { item.resume() }
    entered = []
    if !released { await withCheckedContinuation { waiting = $0 } }
  }
  func wait() async {
    if !reached { await withCheckedContinuation { entered.append($0) } }
  }
  func release() { released = true; waiting?.resume(); waiting = nil }
}

private struct Workspace: Sendable {
  let root: URL
  let temporary: URL
  let applicationSupport: URL
  init() throws {
    root = FileManager.default.temporaryDirectory.appendingPathComponent("hf-tests-\(UUID().uuidString)")
    temporary = root.appendingPathComponent("tmp")
    applicationSupport = root.appendingPathComponent("support")
    try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
  }
  func remove() { try? FileManager.default.removeItem(at: root) }
  func assertClean() throws {
    try expect(try FileManager.default.contentsOfDirectory(atPath: temporary.path).isEmpty, "temporary download survived cleanup")
  }
}

@main
private struct HuggingFaceModelDownloadTests {
  typealias Test = @Sendable () async throws -> Void
  static let revision = String(repeating: "a", count: 40)
  static func main() async {
    let tests: [(String, Test)] = [
      ("metadata, traversal, collisions, extension and budgets", metadata),
      ("nested runtime layouts and approved MLX data", layouts),
      ("streamed Git blob hash is distinct from raw SHA1", integrity),
      ("public HTTPS CDN redirect boundary", redirects),
      ("bounded monotonic progress and terminal callback fence", progress),
      ("nested GGUF download imports, persists origin and removes temp files", ggufImport),
      ("nested MLX import preserves exact remote paths", mlxImport),
      ("CoreML package assembles parent tokenizer sidecars", coreMLImport),
      ("ANEMLL profile rejects partial, mixed and unpinned compiled pipelines", anemllLayouts),
      ("ANEMLL three-component download persists and resolves as one pipeline", anemllImport),
      ("HTTP error and checksum mismatch cannot import", failures),
      ("cancel while downloading cleans the partial operation", downloadCancellation),
      ("cancel while importing leaves the library unchanged", importCancellation),
      ("post-verification source mutation is rejected before promotion", mutationBeforeCopy),
    ]
    var failed = 0
    for (name, test) in tests {
      do { try await test(); print("PASS: \(name)") }
      catch { failed += 1; print("FAIL: \(name): \(error)") }
    }
    print("HuggingFaceModelDownload: \(tests.count - failed)/\(tests.count) tests passed")
    if failed > 0 { Darwin.exit(1) }
  }

  static func file(_ path: String, _ text: String = "abc", git: Bool = false) -> HuggingFaceModelFile {
    let data = Data(text.utf8)
    let sha = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    let blob = Insecure.SHA1.hash(data: Data("blob \(data.count)\0".utf8) + data).map { String(format: "%02x", $0) }.joined()
    return HuggingFaceModelFile(path: path, sizeBytes: Int64(data.count), sha256: git ? nil : sha, gitBlobSha1: git ? blob : nil)
  }

  static func plan(_ runtime: LocalRuntime = .llamaCpp, _ files: [HuggingFaceModelFile] = [file("nested/tiny.gguf")]) throws -> HuggingFaceModelDownload {
    try HuggingFaceModelDownload(runtime: runtime, repoId: "example/public-model", revision: revision, displayName: "Tiny model", files: files)
  }

  static func reject(_ body: () throws -> Void) throws {
    do { try body() } catch is LocalInferenceError { return }
    throw Failure(description: "invalid metadata unexpectedly accepted")
  }

  static func metadata() async throws {
    for path in ["../tiny.gguf", "/tiny.gguf", "a//tiny.gguf", "a/./tiny.gguf", "a/../tiny.gguf", "a\\tiny.gguf", "tiny.gguf?x", "tiny%2f.gguf", "tiny.gguf\n", "a/\u{0000}x.gguf", String(repeating: "a/", count: 17) + "x.gguf"] {
      try reject { _ = try plan(.llamaCpp, [file(path)]) }
    }
    for bad in ["main", "../" + revision, String(repeating: "z", count: 40), revision + "\n"] {
      try reject { _ = try HuggingFaceModelDownload(runtime: .llamaCpp, repoId: "a/b", revision: bad, displayName: "M", files: [file("x.gguf")]) }
    }
    try reject { _ = try HuggingFaceModelDownload(runtime: .llamaCpp, repoId: "example/repo\n", revision: revision, displayName: "M", files: [file("x.gguf")]) }
    try reject { _ = try plan(.llamaCpp, [HuggingFaceModelFile(path: "x.gguf", sizeBytes: 3, sha256: String(repeating: "a", count: 64) + "\n", gitBlobSha1: nil)]) }
    try reject { _ = try plan(.llamaCpp, [HuggingFaceModelFile(path: "x.gguf", sizeBytes: 3, sha256: nil, gitBlobSha1: nil)]) }
    try reject { _ = try plan(.llamaCpp, [HuggingFaceModelFile(path: "x.gguf", sizeBytes: 3, sha256: String(repeating: "a", count: 64), gitBlobSha1: revision)]) }
    for size in [Int64(-1), Int64.max, HuggingFaceModelDownload.maximumBytes + 1] {
      try reject { _ = try plan(.llamaCpp, [HuggingFaceModelFile(path: "x.gguf", sizeBytes: size, sha256: String(repeating: "a", count: 64), gitBlobSha1: nil)]) }
    }
    try reject { _ = try plan(.mlx, (0...512).map { file("w\($0).safetensors") }) }
    try reject { _ = try plan(.mlx, mlxFiles + [file("nested/Config.json")]) }
    for path in ["nested/model.py", "nested/model.pkl", "nested/pytorch_model.bin", "nested/script.sh"] {
      try reject { _ = try plan(.mlx, mlxFiles + [file(path)]) }
    }
  }

  static let mlxFiles = [file("nested/config.json", "{}", git: true), file("nested/tokenizer.json", "{}", git: true), file("nested/model.safetensors")]
  static let coreFiles = [
    file("models/Tiny.mlpackage/Manifest.json", "{}", git: true),
    file("models/Tiny.mlpackage/Data/com.apple.CoreML/model.mlmodel"),
    file("models/Tiny.mlpackage/Data/com.apple.CoreML/weights/weight.bin"),
    file("tokenizer.json", "{}", git: true), file("tokenizer_config.json", "{}", git: true)
  ]

  static func layouts() async throws {
    let gguf = try plan()
    try expect(gguf.localPaths == ["tiny.gguf"], "GGUF nested basename mismatch")
    try expect(gguf.url(for: gguf.origin.files[0]).absoluteString == "https://huggingface.co/example/public-model/resolve/\(revision)/nested/tiny.gguf", "remote path lost")
    let mlx = try plan(.mlx, mlxFiles + [file("nested/vocab.txt"), file("nested/tokenizer.tiktoken"), file("nested/chat_template.jinja"), file("nested/model.safetensors.index.json", "{}")])
    try expect(mlx.localPaths.allSatisfy { $0.hasPrefix("model/") }, "MLX source root mismatch")
    let core = try plan(.coreML, coreFiles)
    try expect(core.localPaths[0] == "model/Tiny.mlpackage/Manifest.json", "CoreML package mapping mismatch")
    try expect(core.localPaths[3] == "model/tokenizer.json", "root tokenizer not assembled")
    try reject { _ = try plan(.coreML, coreFiles + [file("models/tokenizer.json", "{}")]) }
    try reject { _ = try plan(.coreML, coreFiles + [file("other/unrelated.json", "{}")]) }
    try reject { _ = try plan(.coreML, coreFiles + [file("models/Tiny.mlpackage/Data/payload.py")]) }
    try reject { _ = try plan(.coreML, coreFiles + [file("Other.mlpackage/Manifest.json", "{}")]) }
    try reject { _ = try plan(.mlx, [file("a/config.json"), file("b/tokenizer.json"), file("a/weights.safetensors")]) }
  }

  static func integrity() async throws {
    let workspace = try Workspace(); defer { workspace.remove() }
    let path = workspace.temporary.appendingPathComponent("sample")
    try Data("abc".utf8).write(to: path)
    // Independent known Git blob identity: `printf abc | git hash-object --stdin`.
    let gitFile = HuggingFaceModelFile(path: "x.gguf", sizeBytes: 3, sha256: nil, gitBlobSha1: "f2ba8f84ab5c1bce84a7b441cb1959cfc7093b7f")
    let download = try plan(.llamaCpp, [gitFile])
    try expect(try download.verify(gitFile, at: path) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "SHA256 receipt incorrect")
    let rawSHA1 = HuggingFaceModelFile(path: "x.gguf", sizeBytes: 3, sha256: nil, gitBlobSha1: "a9993e364706816aba3e25717850c26c9cd0d89d")
    try reject { _ = try download.verify(rawSHA1, at: path) }
    try Data("abd".utf8).write(to: path)
    try reject { _ = try download.verify(gitFile, at: path) }
    try Data("abcd".utf8).write(to: path)
    try reject { _ = try download.verify(gitFile, at: path) }
    let link = workspace.temporary.appendingPathComponent("link")
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: path)
    try reject { _ = try download.verify(gitFile, at: link) }
    var large = Data(repeating: 0x61, count: 4 * 1_024 * 1_024 + 37)
    let largeGit = Insecure.SHA1.hash(data: Data("blob \(large.count)\0".utf8) + large).map { String(format: "%02x", $0) }.joined()
    let largeFile = HuggingFaceModelFile(path: "x.gguf", sizeBytes: Int64(large.count), sha256: nil, gitBlobSha1: largeGit)
    try large.write(to: path)
    _ = try download.verify(largeFile, at: path)
    large[large.count - 1] = 0x62
    try large.write(to: path)
    try reject { _ = try download.verify(largeFile, at: path) }
  }

  static func redirects() async throws {
    for url in ["https://huggingface.co/file", "https://cdn-lfs.huggingface.co/file", "https://cas-bridge.xethub.hf.co/file?X-Amz-Signature=opaque"] {
      try expect(HuggingFaceModelDownload.allowedRedirect(URL(string: url)), "valid Hub redirect rejected")
    }
    for url in ["http://huggingface.co/file", "https://huggingface.co.attacker.example/file", "https://otherhf.co/file", "https://127.0.0.1/file", "https://user:secret@hf.co/file", "https://hf.co:8443/file"] {
      try expect(!HuggingFaceModelDownload.allowedRedirect(URL(string: url)), "unsafe redirect accepted")
    }
  }

  static func progress() async throws {
    try expect(ModelDownloadProgressTracker().snapshot == ModelDownloadProgress(), "idle is not zero")
    let progress = ModelDownloadProgressTracker(totalBytes: 3, totalFiles: 1)
    try expect(progress.snapshot.state == "downloading", "active tracker reports idle")
    await withTaskGroup(of: Void.self) { group in
      for i in 0..<100 { group.addTask { progress.update(state: "downloading", downloadedBytes: Int64(i), completedFiles: i) } }
    }
    try expect(progress.snapshot.downloadedBytes == 3 && progress.snapshot.completedFiles == 1, "progress exceeded bounds")
    progress.update(state: "verifying")
    progress.receiveBytes(3)
    try expect(progress.snapshot.state == "verifying", "late download callback changed verification phase")
    progress.update(state: "cancelled")
    progress.update(state: "completed", downloadedBytes: 0, completedFiles: 0)
    try expect(progress.snapshot.state == "cancelled", "late callback overwrote cancellation")
  }

  static func fetcher(_ workspace: Workspace, _ contents: [String: String]) -> HuggingFaceModelDownload.Fetch {
    { url, _, callback in
      guard let text = contents.first(where: { url.path.hasSuffix("/" + $0.key) })?.value else {
        throw Failure(description: "unexpected download path")
      }
      let data = Data(text.utf8)
      let temporary = workspace.temporary.appendingPathComponent("network-\(UUID().uuidString)")
      try data.write(to: temporary)
      callback(Int64(data.count))
      return (temporary, 200)
    }
  }

  static func imported(_ runtime: LocalRuntime, files: [HuggingFaceModelFile], contents: [String: String]) async throws {
    let workspace = try Workspace(); defer { workspace.remove() }
    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    let download = try plan(runtime, files)
    let progress = ModelDownloadProgressTracker(totalBytes: download.totalBytes, totalFiles: files.count)
    let result = try await download.downloadAndImport(into: store, progress: progress, temporaryRoot: workspace.temporary, fetch: fetcher(workspace, contents))
    try expect(result.runtime == runtime && result.downloadOrigin == download.origin, "runtime/provenance lost")
    let persisted = try await store.list()
    try expect(persisted.count == 1 && persisted[0].downloadOrigin == download.origin, "origin did not persist")
    let resolved = try await store.resolve(modelId: result.modelId)
    try expect(FileManager.default.fileExists(atPath: resolved.runtimeURL.path), "imported artifact missing")
    try expect(progress.snapshot == ModelDownloadProgress(state: "completed", downloadedBytes: download.totalBytes, totalBytes: download.totalBytes, completedFiles: files.count, totalFiles: files.count), "completion progress not exact")
    try workspace.assertClean()
  }

  static func ggufImport() async throws { try await imported(.llamaCpp, files: [file("nested/tiny.gguf")], contents: ["nested/tiny.gguf": "abc"]) }
  static func mlxImport() async throws { try await imported(.mlx, files: mlxFiles, contents: ["nested/config.json": "{}", "nested/tokenizer.json": "{}", "nested/model.safetensors": "abc"]) }
  static func coreMLImport() async throws {
    try await imported(.coreML, files: coreFiles, contents: Dictionary(uniqueKeysWithValues: coreFiles.map { ($0.path, $0.path.hasSuffix(".json") ? "{}" : "abc") }))
  }

  // File-layout fixtures only: these strings are not executable Core ML graphs.
  static let anemllFiles: [HuggingFaceModelFile] = [
    "llama_embeddings_lut8.mlmodelc", "llama_lm_head_lut8.mlmodelc", "llama_FFN_PF_lut4_chunk_01of01.mlmodelc"
  ].flatMap { component in
    ["analytics/coremldata.bin", "coremldata.bin", "metadata.json", "model.mil", "weights/weight.bin"].map {
      file("pinned/" + component + "/" + $0)
    }
  } + [file("pinned/config.json"), file("pinned/tokenizer.json"), file("pinned/tokenizer_config.json")]

  static func anemllPlan(_ files: [HuggingFaceModelFile]) throws -> HuggingFaceModelDownload {
    try HuggingFaceModelDownload(runtime: .coreML, repoId: "anemll/anemll-Llama-3.2-1B-FAST-iOS_0.3.0",
      revision: "c6461a77a6f803424ec347f9537aadac37094879", displayName: "ANEMLL 1B", files: files)
  }

  static func anemllLayouts() async throws {
    let accepted = try anemllPlan(anemllFiles)
    try expect(accepted.localPaths.count == 18 && accepted.localPaths.contains("model/llama_FFN_PF_lut4_chunk_01of01.mlmodelc/model.mil"), "pipeline component lost")
    try expect(accepted.localPaths.contains("model/config.json"), "pipeline config lost")
    try reject { _ = try plan(.coreML, anemllFiles) }
    try reject { _ = try HuggingFaceModelDownload(runtime: .coreML, repoId: ANEMLLModelProfile.repository,
      revision: revision, displayName: "other revision", files: anemllFiles) }
    for bad in [
      Array(anemllFiles.dropFirst()),
      Array(anemllFiles.dropLast()),
      anemllFiles + [file("pinned/llama_embeddings_lut8.mlmodelc/script.py")],
      anemllFiles + [file("pinned/Other.mlpackage/Manifest.json")],
      anemllFiles + [file("other/tokenizer.json")],
      anemllFiles + [file("pinned/tokenizer.py")],
      anemllFiles.filter { !$0.path.hasSuffix("weights/weight.bin") } + [file("pinned/llama_embeddings_lut8.mlmodelc/weights/weight.bin", "")]
    ] { try reject { _ = try anemllPlan(bad) } }
  }

  static func anemllImport() async throws {
    let workspace = try Workspace(); defer { workspace.remove() }
    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    let download = try anemllPlan(anemllFiles)
    let tracker = ModelDownloadProgressTracker(totalBytes: download.totalBytes, totalFiles: anemllFiles.count)
    let contents = Dictionary(uniqueKeysWithValues: anemllFiles.map { ($0.path, "abc") })
    let record = try await download.downloadAndImport(into: store, progress: tracker,
      temporaryRoot: workspace.temporary, fetch: fetcher(workspace, contents))
    let resolved = try await store.resolve(modelId: record.modelId)
    try ANEMLLModelProfile.validateDirectory(resolved.runtimeURL)
    try expect(resolved.runtimeURL == resolved.tokenizerURL, "pipeline resolved to one component instead of shared root")
    try expect(record.downloadOrigin == download.origin, "pinned pipeline provenance lost")
    for path in download.localPaths {
      let local = String(path.dropFirst("model/".count))
      try expect(try String(contentsOf: resolved.runtimeURL.appendingPathComponent(local), encoding: .utf8) == "abc", "component changed during import")
    }
    let restarted = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    let afterRestart = try await restarted.resolve(modelId: record.modelId)
    try ANEMLLModelProfile.validateDirectory(afterRestart.runtimeURL)
    try expect(afterRestart.runtimeURL == resolved.runtimeURL && afterRestart.stored == resolved.stored, "pipeline did not survive store reload")
    try expect(tracker.snapshot.state == "completed", "pipeline download did not complete")
    try workspace.assertClean()
  }

  static func failures() async throws {
    for status in [200, 403] {
      let workspace = try Workspace(); defer { workspace.remove() }
      let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
      let download = try plan()
      let progress = ModelDownloadProgressTracker(totalBytes: 3, totalFiles: 1)
      do {
        _ = try await download.downloadAndImport(into: store, progress: progress, temporaryRoot: workspace.temporary) { _, _, _ in
          let temporary = workspace.temporary.appendingPathComponent("bad-file")
          try Data("bad".utf8).write(to: temporary)
          return (temporary, status)
        }
        throw Failure(description: "invalid response imported")
      } catch is LocalInferenceError {}
      let remaining = try await store.list()
      try expect(remaining.isEmpty, "failed download changed library")
      try expect(progress.snapshot.state == "failed", "failed state missing")
      try workspace.assertClean()
    }
  }

  static func downloadCancellation() async throws {
    let workspace = try Workspace(); defer { workspace.remove() }
    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    let download = try plan()
    let progress = ModelDownloadProgressTracker(totalBytes: 3, totalFiles: 1)
    let gate = Gate()
    let task = Task {
      try await download.downloadAndImport(into: store, progress: progress, temporaryRoot: workspace.temporary) { _, _, _ in
        await gate.pause()
        try Task.checkCancellation()
        throw Failure(description: "cancelled fetch completed")
      }
    }
    await gate.wait(); task.cancel(); await gate.release()
    do { _ = try await task.value; throw Failure(description: "cancel returned success") } catch is CancellationError {}
    let remaining = try await store.list()
    try expect(remaining.isEmpty, "cancelled download changed library")
    try expect(progress.snapshot.state == "cancelled", "cancel terminal state missing")
    try workspace.assertClean()
  }

  static func importCancellation() async throws {
    let workspace = try Workspace(); defer { workspace.remove() }
    let gate = Gate()
    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport, importDidCreateStaging: { await gate.pause() })
    let download = try plan()
    let progress = ModelDownloadProgressTracker(totalBytes: 3, totalFiles: 1)
    let task = Task { try await download.downloadAndImport(into: store, progress: progress, temporaryRoot: workspace.temporary, fetch: fetcher(workspace, ["nested/tiny.gguf": "abc"])) }
    await gate.wait()
    try expect(progress.snapshot.state == "importing", "import stage not observable")
    task.cancel(); await gate.release()
    do { _ = try await task.value; throw Failure(description: "cancelled import returned success") } catch is CancellationError {}
    let remaining = try await store.list()
    try expect(remaining.isEmpty, "cancelled import committed")
    try expect(progress.snapshot.state == "cancelled", "cancelled import state missing")
    try workspace.assertClean()
  }

  static func mutationBeforeCopy() async throws {
    let workspace = try Workspace(); defer { workspace.remove() }
    let store = LocalModelStore(applicationSupportURL: workspace.applicationSupport)
    let download = try plan(.mlx, mlxFiles)
    let progress = ModelDownloadProgressTracker(totalBytes: download.totalBytes, totalFiles: mlxFiles.count)
    do {
      _ = try await download.downloadAndImport(into: store, progress: progress, temporaryRoot: workspace.temporary) { url, _, _ in
        let isWeight = url.path.hasSuffix(".safetensors")
        if isWeight {
          let staged = try FileManager.default.contentsOfDirectory(at: workspace.temporary, includingPropertiesForKeys: nil).first { $0.lastPathComponent.hasPrefix("swarmer-hf-") }!
          // Same size, changed before store records source identity: only the copy digest detects it.
          try Data("[]".utf8).write(to: staged.appendingPathComponent("model/config.json"))
        }
        let path = workspace.temporary.appendingPathComponent("network-\(UUID().uuidString)")
        try Data((isWeight ? "abc" : "{}").utf8).write(to: path)
        return (path, 200)
      }
      throw Failure(description: "mutated source promoted")
    } catch LocalInferenceError.sourceChangedDuringImport {}
    let remaining = try await store.list()
    try expect(remaining.isEmpty, "mutation changed library")
    try expect(progress.snapshot.state == "failed", "mutation not marked failed")
    try workspace.assertClean()
  }
}
