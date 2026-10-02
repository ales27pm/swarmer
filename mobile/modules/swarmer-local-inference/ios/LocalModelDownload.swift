import CryptoKit
import Foundation

struct HuggingFaceModelFile: Codable, Equatable, Sendable {
  let path: String
  let sizeBytes: Int64
  let sha256: String?
  let gitBlobSha1: String?
}

struct StoredModelDownloadOrigin: Codable, Equatable, Sendable {
  let repoId: String
  let revision: String
  let files: [HuggingFaceModelFile]
}

struct ModelDownloadProgress: Equatable, Sendable {
  var state = "idle"
  var downloadedBytes: Int64 = 0
  var totalBytes: Int64 = 0
  var completedFiles = 0
  var totalFiles = 0
}

/// Each operation owns a tracker, so late delegate callbacks cannot affect a later download.
final class ModelDownloadProgressTracker: @unchecked Sendable {
  private let lock = NSLock()
  private var value: ModelDownloadProgress

  init(totalBytes: Int64 = 0, totalFiles: Int = 0) {
    value = ModelDownloadProgress(state: totalFiles > 0 ? "downloading" : "idle", totalBytes: totalBytes, totalFiles: totalFiles)
  }

  var snapshot: ModelDownloadProgress { lock.withLock { value } }

  func receiveBytes(_ bytes: Int64) {
    lock.withLock {
      guard value.state == "downloading" else { return }
      value.downloadedBytes = max(value.downloadedBytes, min(max(0, bytes), value.totalBytes))
    }
  }

  func update(state: String, downloadedBytes: Int64? = nil, completedFiles: Int? = nil) {
    lock.withLock {
      guard !["completed", "cancelled", "failed"].contains(value.state) else { return }
      value.state = state
      if let downloadedBytes {
        value.downloadedBytes = max(value.downloadedBytes, min(max(0, downloadedBytes), value.totalBytes))
      }
      if let completedFiles {
        value.completedFiles = max(value.completedFiles, min(max(0, completedFiles), value.totalFiles))
      }
    }
  }
}

/// The bridge supplies identities, never arbitrary URLs, credentials, or executable repository files.
struct HuggingFaceModelDownload: Sendable {
  static let maximumBytes = LocalModelDownload.maximumBytes
  static let maximumFiles = 512
  static let sidecars: Set<String> = [
    "config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "added_tokens.json", "tokenizer.model", "merges.txt", "vocab.json"
  ]
  let runtime: LocalRuntime
  let origin: StoredModelDownloadOrigin
  let displayName: String
  let totalBytes: Int64
  /// Paths within the import payload (not necessarily the remote repository layout).
  let localPaths: [String]

  init(runtime: LocalRuntime, repoId: String, revision: String, displayName: String, files: [HuggingFaceModelFile]) throws {
    guard repoId.range(of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}\\z", options: .regularExpression) != nil,
          revision.range(of: "^[a-fA-F0-9]{40}\\z", options: .regularExpression) != nil,
          !files.isEmpty, files.count <= Self.maximumFiles else {
      throw LocalInferenceError.invalidDownloadMetadata
    }
    let name = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !name.isEmpty, name.count <= 120, name.rangeOfCharacter(from: .controlCharacters) == nil else {
      throw LocalInferenceError.invalidDisplayName
    }
    var total: Int64 = 0
    var paths = Set<String>()
    for file in files {
      guard Self.safePath(file.path), paths.insert(file.path.lowercased()).inserted,
            (file.sha256 == nil) != (file.gitBlobSha1 == nil),
            file.sha256.map({ $0.range(of: "^[a-fA-F0-9]{64}\\z", options: .regularExpression) != nil }) ?? true,
            file.gitBlobSha1.map({ $0.range(of: "^[a-fA-F0-9]{40}\\z", options: .regularExpression) != nil }) ?? true else {
        throw LocalInferenceError.invalidDownloadMetadata
      }
      let (sum, overflow) = total.addingReportingOverflow(file.sizeBytes)
      guard file.sizeBytes >= 0, !overflow, sum <= Self.maximumBytes else { throw LocalInferenceError.importTooLarge }
      total = sum
    }
    guard total > 0 else { throw LocalInferenceError.invalidDownloadMetadata }
    let localPaths = try Self.assemblyPaths(runtime: runtime, files: files)
    guard Set(localPaths.map { $0.lowercased() }).count == localPaths.count else {
      throw LocalInferenceError.invalidDownloadMetadata
    }
    for path in paths {
      let parts = path.split(separator: "/")
      for end in 1..<parts.count where paths.contains(parts.prefix(end).joined(separator: "/")) {
        throw LocalInferenceError.invalidDownloadMetadata
      }
    }
    self.runtime = runtime
    self.origin = StoredModelDownloadOrigin(repoId: repoId, revision: revision.lowercased(), files: files.map {
      HuggingFaceModelFile(path: $0.path, sizeBytes: $0.sizeBytes, sha256: $0.sha256?.lowercased(), gitBlobSha1: $0.gitBlobSha1?.lowercased())
    })
    self.displayName = name
    self.totalBytes = total
    self.localPaths = localPaths
  }

  static func safePath(_ path: String) -> Bool {
    let parts = path.split(separator: "/", omittingEmptySubsequences: false)
    return path.utf8.count <= 1_024 && !parts.isEmpty && parts.count <= 16 && parts.allSatisfy {
      $0 != "." && $0 != ".." && $0.range(of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,239}\\z", options: .regularExpression) != nil
    }
  }

  private static func assemblyPaths(runtime: LocalRuntime, files: [HuggingFaceModelFile]) throws -> [String] {
    let paths = files.map(\.path)
    let names = paths.map { ($0 as NSString).lastPathComponent }
    switch runtime {
    case .llamaCpp:
      guard paths.count == 1, paths[0].lowercased().hasSuffix(".gguf"), files[0].sizeBytes > 0 else {
        throw LocalInferenceError.invalidDownloadMetadata
      }
      return names
    case .mlx:
      let parents = Set(paths.map { ($0 as NSString).deletingLastPathComponent })
      guard parents.count == 1, names.contains("config.json"), names.contains("tokenizer.json"),
            files.contains(where: { $0.path.hasSuffix(".safetensors") && $0.sizeBytes > 0 }),
            names.allSatisfy({ sidecars.contains($0) || ["vocab.txt", "tokenizer.tiktoken", "chat_template.jinja"].contains($0)
              || $0.hasSuffix(".safetensors.index.json") || $0.hasSuffix(".safetensors") }) else {
        throw LocalInferenceError.invalidDownloadMetadata
      }
      return names.map { "model/\($0)" }
    case .coreML:
      let packages = Set(paths.compactMap { path -> String? in
        let parts = path.split(separator: "/")
        guard let end = parts.firstIndex(where: { $0.hasSuffix(".mlpackage") }) else { return nil }
        return parts.prefix(end + 1).joined(separator: "/")
      })
      guard packages.count == 1, let package = packages.first,
            paths.contains("\(package)/Manifest.json") else { throw LocalInferenceError.invalidDownloadMetadata }
      let packageName = (package as NSString).lastPathComponent
      let parent = (package as NSString).deletingLastPathComponent
      var sidecarNames = Set<String>()
      let result = try paths.map { path -> String in
        if path.hasPrefix(package + "/") {
          let relative = String(path.dropFirst(package.count + 1))
          guard relative == "Manifest.json" || (relative.hasPrefix("Data/") &&
            ["mlmodel", "bin"].contains((relative as NSString).pathExtension)) else {
            throw LocalInferenceError.invalidDownloadMetadata
          }
          return "model/\(packageName)/\(relative)"
        }
        let name = (path as NSString).lastPathComponent
        let sidecarParent = (path as NSString).deletingLastPathComponent
        guard sidecars.contains(name), sidecarNames.insert(name).inserted,
              sidecarParent.isEmpty || sidecarParent == parent || parent.hasPrefix(sidecarParent + "/") else {
          throw LocalInferenceError.invalidDownloadMetadata
        }
        return "model/\(name)"
      }
      guard sidecarNames.isSuperset(of: ["tokenizer.json", "tokenizer_config.json"]),
            paths.contains(where: { $0.hasPrefix(package + "/Data/") && $0.hasSuffix(".mlmodel") }) else {
        throw LocalInferenceError.invalidDownloadMetadata
      }
      return result
    }
  }

  func url(for file: HuggingFaceModelFile) -> URL {
    // Every interpolated component was restricted to an ASCII path alphabet above.
    URL(string: "https://huggingface.co/\(origin.repoId)/resolve/\(origin.revision)/\(file.path)")!
  }

  /// Also computes SHA256 for the store's independent copy verification when Hub uses Git SHA1.
  func verify(_ file: HuggingFaceModelFile, at url: URL) throws -> String {
    try Task.checkCancellation()
    var inspectedURL = url
    inspectedURL.removeAllCachedResourceValues()
    let values = try inspectedURL.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey])
    guard values.isRegularFile == true, values.isSymbolicLink != true,
          values.fileSize.map(Int64.init) == file.sizeBytes else { throw LocalInferenceError.downloadSizeMismatch }
    let input = try FileHandle(forReadingFrom: url)
    defer { try? input.close() }
    var sha256 = SHA256()
    var git = Insecure.SHA1()
    git.update(data: Data("blob \(file.sizeBytes)\0".utf8))
    var count: Int64 = 0
    while true {
      try Task.checkCancellation()
      let hasData = try autoreleasepool {
        guard let data = try input.read(upToCount: 4 * 1_024 * 1_024), !data.isEmpty else { return false }
        count += Int64(data.count)
        guard count <= file.sizeBytes else { throw LocalInferenceError.downloadSizeMismatch }
        sha256.update(data: data)
        if file.gitBlobSha1 != nil { git.update(data: data) }
        return true
      }
      if !hasData { break }
    }
    guard count == file.sizeBytes else { throw LocalInferenceError.downloadSizeMismatch }
    let actual = sha256.finalize().map { String(format: "%02x", $0) }.joined()
    let gitActual = git.finalize().map { String(format: "%02x", $0) }.joined()
    guard file.sha256.map({ actual == $0 }) ?? (gitActual == file.gitBlobSha1) else {
      throw LocalInferenceError.downloadChecksumMismatch
    }
    return actual
  }

  typealias Fetch = @Sendable (URL, Int64, @escaping @Sendable (Int64) -> Void) async throws -> (URL, Int)

  func downloadAndImport(
    into store: LocalModelStore,
    progress: ModelDownloadProgressTracker,
    temporaryRoot: URL = FileManager.default.temporaryDirectory,
    fetch: Fetch = Self.fetch
  ) async throws -> StoredLocalModel {
    do {
      try Task.checkCancellation()
      let capacity = try temporaryRoot.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage
      guard let capacity, capacity >= totalBytes * 2 + 1_024 * 1_024 * 1_024 else { throw LocalInferenceError.insufficientStorage }
      let directory = temporaryRoot.appendingPathComponent("swarmer-hf-\(UUID().uuidString)", isDirectory: true)
      let manager = FileManager.default
      try manager.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
      defer { try? manager.removeItem(at: directory) }
      var artifacts: [StoredModelArtifact] = []
      var verifiedBytes: Int64 = 0
      for (index, file) in origin.files.enumerated() {
        try Task.checkCancellation()
        progress.update(state: "downloading")
        let baseBytes = verifiedBytes
        let (temporaryFile, status) = try await fetch(url(for: file), file.sizeBytes, { partialBytes in
          progress.receiveBytes(baseBytes + min(max(0, partialBytes), file.sizeBytes))
        })
        defer { try? manager.removeItem(at: temporaryFile) }
        try Task.checkCancellation()
        guard status == 200 else { throw LocalInferenceError.modelDownloadFailed }
        progress.update(state: "verifying")
        let digest = try verify(file, at: temporaryFile)
        let relative = localPaths[index]
        let destination = directory.appendingPathComponent(relative)
        try manager.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try manager.moveItem(at: temporaryFile, to: destination)
        #if os(iOS)
        try manager.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: destination.path)
        #endif
        artifacts.append(StoredModelArtifact(filename: relative, sizeBytes: file.sizeBytes, sha256: digest))
        verifiedBytes += file.sizeBytes
        progress.update(state: "verifying", downloadedBytes: verifiedBytes, completedFiles: index + 1)
      }
      try Task.checkCancellation()
      progress.update(state: "importing")
      let source = directory.appendingPathComponent(runtime == .llamaCpp ? localPaths[0] : "model")
      let imported = try await store.importDownloadedModel(
        runtime: runtime, source: source, displayName: displayName, origin: origin, expectedArtifacts: artifacts
      )
      progress.update(state: "completed")
      return imported
    } catch {
      progress.update(state: Task.isCancelled || error is CancellationError ? "cancelled" : "failed")
      throw error
    }
  }

  static func allowedRedirect(_ url: URL?) -> Bool {
    guard let url, url.scheme == "https", url.user == nil, url.password == nil,
          url.port == nil || url.port == 443, let host = url.host?.lowercased() else { return false }
    return ["huggingface.co", "hf.co"].contains(where: { host == $0 || host.hasSuffix("." + $0) })
  }

  private static func fetch(_ url: URL, maximumBytes: Int64, progress: @escaping @Sendable (Int64) -> Void) async throws -> (URL, Int) {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.urlCache = nil
    configuration.httpCookieStorage = nil
    configuration.urlCredentialStorage = nil
    configuration.timeoutIntervalForRequest = 60
    configuration.timeoutIntervalForResource = 3_600
    let session = URLSession(configuration: configuration)
    defer { session.invalidateAndCancel() }
    let delegate = LocalModelDownloadDelegate(maximumBytes: maximumBytes, progress: progress, restrictHosts: true)
    do {
      let (file, response) = try await session.download(from: url, delegate: delegate)
      if delegate.exceededSize {
        try? FileManager.default.removeItem(at: file)
        throw LocalInferenceError.downloadSizeMismatch
      }
      return (file, (response as? HTTPURLResponse)?.statusCode ?? 0)
    } catch {
      try Task.checkCancellation()
      if delegate.exceededSize { throw LocalInferenceError.downloadSizeMismatch }
      throw LocalInferenceError.modelDownloadFailed
    }
  }
}

/// A remote file is accepted only with an immutable identity and an exact byte budget.
struct LocalModelDownload: Sendable {
  static let maximumBytes: Int64 = 16 * 1_024 * 1_024 * 1_024
  private static let reserveBytes: Int64 = 1_024 * 1_024 * 1_024
  let url: URL
  let filename: String
  let sha256: String
  let sizeBytes: Int64
  let displayName: String

  init(
    repoId: String,
    revision: String,
    filename: String,
    sha256: String,
    sizeBytes: Int64,
    displayName: String
  ) throws {
    guard repoId.range(
      of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$",
      options: .regularExpression
    ) != nil,
    revision.range(of: "^[0-9A-Fa-f]{40}\\z", options: .regularExpression) != nil,
    filename.range(of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,239}\\.gguf\\z", options: .regularExpression) != nil,
    sha256.range(of: "^[0-9A-Fa-f]{64}\\z", options: .regularExpression) != nil else {
      throw LocalInferenceError.invalidDownloadMetadata
    }
    guard sizeBytes > 0, sizeBytes <= Self.maximumBytes else {
      throw LocalInferenceError.importTooLarge
    }
    let name = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !name.isEmpty, name.count <= 120,
          name.rangeOfCharacter(from: .controlCharacters) == nil else {
      throw LocalInferenceError.invalidDisplayName
    }
    guard let url = URL(string: "https://huggingface.co/\(repoId)/resolve/\(revision.lowercased())/\(filename)") else {
      throw LocalInferenceError.invalidDownloadMetadata
    }
    self.url = url
    self.filename = filename
    self.sha256 = sha256.lowercased()
    self.sizeBytes = sizeBytes
    self.displayName = name
  }

  func downloadAndImport(into store: LocalModelStore) async throws -> StoredLocalModel {
    try Task.checkCancellation()
    let fileManager = FileManager.default
    let temporaryRoot = fileManager.temporaryDirectory
    // Download and private-store copy coexist until import finishes.
    let capacity = try temporaryRoot.resourceValues(
      forKeys: [.volumeAvailableCapacityForImportantUsageKey]
    ).volumeAvailableCapacityForImportantUsage
    guard let capacity, capacity >= sizeBytes * 2 + Self.reserveBytes else {
      throw LocalInferenceError.insufficientStorage
    }
    let directory = temporaryRoot.appendingPathComponent("swarmer-download-\(UUID().uuidString)", isDirectory: true)
    try fileManager.createDirectory(at: directory, withIntermediateDirectories: false)
    defer { try? fileManager.removeItem(at: directory) }

    let configuration = URLSessionConfiguration.ephemeral
    configuration.urlCache = nil
    configuration.httpCookieStorage = nil
    configuration.urlCredentialStorage = nil
    configuration.timeoutIntervalForRequest = 60
    configuration.timeoutIntervalForResource = 3_600
    let session = URLSession(configuration: configuration)
    defer { session.invalidateAndCancel() }
    let delegate = LocalModelDownloadDelegate(maximumBytes: sizeBytes)
    let temporaryFile: URL
    let response: URLResponse
    do {
      (temporaryFile, response) = try await session.download(from: url, delegate: delegate)
    } catch {
      try Task.checkCancellation()
      if delegate.exceededSize { throw LocalInferenceError.downloadSizeMismatch }
      throw error
    }
    defer { try? fileManager.removeItem(at: temporaryFile) }
    try Task.checkCancellation()
    guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
      throw LocalInferenceError.modelDownloadFailed
    }
    try verifyDownloadedFile(at: temporaryFile)
    let namedFile = directory.appendingPathComponent(filename, isDirectory: false)
    try fileManager.moveItem(at: temporaryFile, to: namedFile)
    #if os(iOS)
    try fileManager.setAttributes([.protectionKey: FileProtectionType.complete], ofItemAtPath: namedFile.path)
    #endif
    try Task.checkCancellation()
    return try await store.importModel(
      runtime: .llamaCpp,
      uri: namedFile.absoluteString,
      displayName: displayName
    )
  }

  func verifyDownloadedFile(at file: URL) throws {
    try Task.checkCancellation()
    let values = try file.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey])
    guard values.isRegularFile == true, values.isSymbolicLink != true,
          values.fileSize.map(Int64.init) == sizeBytes else {
      throw LocalInferenceError.downloadSizeMismatch
    }
    let handle = try FileHandle(forReadingFrom: file)
    defer { try? handle.close() }
    var digest = SHA256()
    var readBytes: Int64 = 0
    while true {
      try Task.checkCancellation()
      guard let chunk = try handle.read(upToCount: 4 * 1_024 * 1_024), !chunk.isEmpty else { break }
      readBytes += Int64(chunk.count)
      guard readBytes <= sizeBytes else { throw LocalInferenceError.downloadSizeMismatch }
      digest.update(data: chunk)
    }
    guard readBytes == sizeBytes else { throw LocalInferenceError.downloadSizeMismatch }
    let actual = digest.finalize().map { String(format: "%02x", $0) }.joined()
    guard actual == sha256 else { throw LocalInferenceError.downloadChecksumMismatch }
    try Task.checkCancellation()
  }
}

private final class LocalModelDownloadDelegate: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
  // Delegate callbacks and the awaiting task share only lock-protected failure state.
  private let maximumBytes: Int64
  private let lock = NSLock()
  private var oversized = false
  private let progress: @Sendable (Int64) -> Void
  private let restrictHosts: Bool

  init(maximumBytes: Int64, progress: @escaping @Sendable (Int64) -> Void = { _ in }, restrictHosts: Bool = false) {
    self.maximumBytes = maximumBytes
    self.progress = progress
    self.restrictHosts = restrictHosts
  }

  var exceededSize: Bool {
    lock.withLock { oversized }
  }

  func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {}

  func urlSession(
    _ session: URLSession,
    downloadTask: URLSessionDownloadTask,
    didWriteData bytesWritten: Int64,
    totalBytesWritten: Int64,
    totalBytesExpectedToWrite: Int64
  ) {
    progress(min(totalBytesWritten, maximumBytes))
    guard totalBytesWritten > maximumBytes || totalBytesExpectedToWrite > maximumBytes else { return }
    lock.withLock { oversized = true }
    downloadTask.cancel()
  }

  func urlSession(
    _ session: URLSession,
    task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse,
    newRequest request: URLRequest,
    completionHandler: @escaping @Sendable (URLRequest?) -> Void
  ) {
    // Hugging Face redirects LFS downloads to its CDN; never follow a downgrade.
    let allowed = restrictHosts ? HuggingFaceModelDownload.allowedRedirect(request.url) : request.url?.scheme == "https"
    completionHandler(allowed ? request : nil)
  }
}
