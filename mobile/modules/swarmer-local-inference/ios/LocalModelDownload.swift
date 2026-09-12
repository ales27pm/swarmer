import CryptoKit
import Foundation

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
    revision.range(of: "^[0-9A-Fa-f]{40}$", options: .regularExpression) != nil,
    filename.range(of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,239}\\.gguf$", options: .regularExpression) != nil,
    sha256.range(of: "^[0-9A-Fa-f]{64}$", options: .regularExpression) != nil else {
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

  init(maximumBytes: Int64) {
    self.maximumBytes = maximumBytes
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
    completionHandler(request.url?.scheme == "https" ? request : nil)
  }
}
