#if DEBUG
import CryptoKit
import Darwin
import Foundation

/// Only a manifest in the signed application bundle authorizes this diagnostic import.
/// Neither a URL nor a manifest supplied by the automation caller is accepted.
enum CoreMLDiagnosticCandidate {
  static let directoryName = "CoreMLDiagnosticCandidate"
  static let manifestName = "CoreMLDiagnosticCandidateManifest"
  static let maximumBytes: Int64 = 4 * 1_024 * 1_024 * 1_024
  static let maximumManifestBytes = 256 * 1_024
  private static let maximumFiles = 256
  private static let maximumDepth = 12
  private static let chunkBytes = 4 * 1_024 * 1_024
  private static let sidecars: Set<String> = [
    "config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "added_tokens.json", "tokenizer.model", "merges.txt", "vocab.json"
  ]

  enum Failure: String, Error, LocalizedError {
    case unavailable, invalidManifest, invalidInventory, checksumMismatch, sourceChanged
    var errorDescription: String? { "coreml_diagnostic_candidate_\(rawValue)" }
  }

  struct Manifest: Decodable, Sendable {
    struct File: Decodable, Sendable {
      let path: String
      let sizeBytes: Int64
      let sha256: String
    }
    let schemaVersion: Int
    let candidateId: String
    let displayName: String
    let modelDirectory: String
    let files: [File]

    var expectedArtifacts: [StoredModelArtifact] {
      files.map { StoredModelArtifact(
        filename: "\(directoryName)/\($0.path)", sizeBytes: $0.sizeBytes, sha256: $0.sha256
      ) }
    }
  }

  static var isAvailable: Bool { (try? bundledManifest()) != nil }

  static func bundledManifest() throws -> Manifest {
    guard let url = Bundle.main.url(forResource: manifestName, withExtension: "json"),
          let size = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize,
          size > 0, size <= maximumManifestBytes else { throw Failure.unavailable }
    do { return try decodeManifest(Data(contentsOf: url)) }
    catch { throw Failure.invalidManifest }
  }

  static func decodeManifest(_ data: Data) throws -> Manifest {
    guard !data.isEmpty, data.count <= maximumManifestBytes else { throw Failure.invalidManifest }
    let manifest: Manifest
    do { manifest = try JSONDecoder().decode(Manifest.self, from: data) }
    catch { throw Failure.invalidManifest }
    guard manifest.schemaVersion == 1,
          validComponent(manifest.candidateId), manifest.candidateId.count <= 80,
          !manifest.displayName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
          manifest.displayName.count <= 120,
          !manifest.displayName.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
          validComponent(manifest.modelDirectory), manifest.modelDirectory.hasSuffix(".mlpackage"),
          !manifest.files.isEmpty, manifest.files.count <= maximumFiles else { throw Failure.invalidManifest }
    var paths: Set<String> = []
    var total: Int64 = 0
    for file in manifest.files {
      let components = file.path.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
      guard !components.isEmpty, components.count <= maximumDepth, file.path.count <= 512,
            components.allSatisfy(validComponent),
            (components.count > 1 && components[0] == manifest.modelDirectory)
              || (components.count == 1 && sidecars.contains(file.path)),
            paths.insert(file.path).inserted,
            file.sizeBytes >= 0, file.sizeBytes <= maximumBytes,
            file.sha256.utf8.count == 64,
            file.sha256.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else {
        throw Failure.invalidManifest
      }
      let (sum, overflow) = total.addingReportingOverflow(file.sizeBytes)
      guard !overflow, sum <= maximumBytes else { throw Failure.invalidManifest }
      total = sum
    }
    guard total > 0, paths.contains("tokenizer.json"), paths.contains("tokenizer_config.json"),
          paths.contains(where: { $0.hasPrefix(manifest.modelDirectory + "/") }) else {
      throw Failure.invalidManifest
    }
    return manifest
  }

  /// Fixed child of Documents; exact inventory prevents silently ignoring extra files.
  /// Hashing uses no-follow descriptors and bounded chunks, never a whole weight file.
  static func verify(documentsURL: URL, manifest: Manifest) throws -> URL {
    try Task.checkCancellation()
    let documentsFD = Darwin.open(documentsURL.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard documentsFD >= 0 else { throw Failure.invalidInventory }
    defer { Darwin.close(documentsFD) }
    let rootFD = Darwin.openat(documentsFD, directoryName, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard rootFD >= 0 else { throw Failure.invalidInventory }
    defer { Darwin.close(rootFD) }
    let root = documentsURL.appendingPathComponent(directoryName, isDirectory: true)
    let expectedFiles = Set(manifest.files.map(\.path))
    var expectedDirectories: Set<String> = []
    for path in expectedFiles {
      var components = path.split(separator: "/").map(String.init)
      components.removeLast()
      while !components.isEmpty {
        expectedDirectories.insert(components.joined(separator: "/"))
        components.removeLast()
      }
    }
    var observedFiles: Set<String> = []
    var observedDirectories: Set<String> = []
    try inventory(
      root, relativePath: "", expectedFiles: expectedFiles, expectedDirectories: expectedDirectories,
      files: &observedFiles, directories: &observedDirectories
    )
    guard observedFiles == expectedFiles, observedDirectories == expectedDirectories else {
      throw Failure.invalidInventory
    }
    for file in manifest.files {
      try Task.checkCancellation()
      let descriptor = try openFile(path: file.path, rootFD: rootFD)
      let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
      defer { try? handle.close() }
      var before = stat()
      guard fstat(descriptor, &before) == 0, before.st_mode & S_IFMT == S_IFREG,
            before.st_size == file.sizeBytes else { throw Failure.invalidInventory }
      var digest = SHA256()
      var count: Int64 = 0
      while true {
        try Task.checkCancellation()
        let hasData = try autoreleasepool {
          guard let data = try handle.read(upToCount: chunkBytes), !data.isEmpty else { return false }
          count += Int64(data.count)
          guard count <= file.sizeBytes else { throw Failure.sourceChanged }
          digest.update(data: data)
          return true
        }
        if !hasData { break }
      }
      var after = stat()
      guard count == file.sizeBytes, fstat(descriptor, &after) == 0,
            before.st_dev == after.st_dev, before.st_ino == after.st_ino,
            before.st_size == after.st_size,
            before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
            before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
            before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec,
            before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec else { throw Failure.sourceChanged }
      let hash = digest.finalize().map { String(format: "%02x", $0) }.joined()
      guard hash == file.sha256 else { throw Failure.checksumMismatch }
    }
    return root
  }

  private static func inventory(
    _ directory: URL, relativePath: String, expectedFiles: Set<String>, expectedDirectories: Set<String>,
    files: inout Set<String>, directories: inout Set<String>
  ) throws {
    try Task.checkCancellation()
    let children: [URL]
    do { children = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil) }
    catch { throw Failure.invalidInventory }
    guard children.count <= maximumFiles + expectedDirectories.count else { throw Failure.invalidInventory }
    for child in children {
      try Task.checkCancellation()
      let path = relativePath.isEmpty ? child.lastPathComponent : relativePath + "/" + child.lastPathComponent
      var attributes = stat()
      guard lstat(child.path, &attributes) == 0 else { throw Failure.invalidInventory }
      switch attributes.st_mode & S_IFMT {
      case S_IFDIR:
        guard expectedDirectories.contains(path), directories.insert(path).inserted else { throw Failure.invalidInventory }
        try inventory(child, relativePath: path, expectedFiles: expectedFiles, expectedDirectories: expectedDirectories,
                      files: &files, directories: &directories)
      case S_IFREG:
        guard expectedFiles.contains(path), files.insert(path).inserted else { throw Failure.invalidInventory }
      default: throw Failure.invalidInventory
      }
    }
  }

  private static func openFile(path: String, rootFD: Int32) throws -> Int32 {
    let components = path.split(separator: "/").map(String.init)
    var parent = Darwin.dup(rootFD)
    guard parent >= 0 else { throw Failure.invalidInventory }
    defer { Darwin.close(parent) }
    for component in components.dropLast() {
      let next = Darwin.openat(parent, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
      guard next >= 0 else { throw Failure.invalidInventory }
      Darwin.close(parent)
      parent = next
    }
    guard let filename = components.last else { throw Failure.invalidInventory }
    let descriptor = Darwin.openat(parent, filename, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
    guard descriptor >= 0 else { throw Failure.invalidInventory }
    return descriptor
  }

  private static func validComponent(_ value: String) -> Bool {
    !value.isEmpty && value != "." && value != ".." && value.utf8.count <= 255
      && value.utf8.allSatisfy {
        (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || [45, 46, 95].contains($0)
      }
  }
}
#endif
