import Foundation
import Darwin
import CryptoKit

actor LocalModelStore {
  private struct SourceIdentity: Equatable, Sendable {
    let device: UInt64
    let inode: UInt64
    let sizeBytes: Int64
    let modifiedSeconds: Int64
    let modifiedNanoseconds: Int64
    let changedSeconds: Int64
    let changedNanoseconds: Int64
  }

  private struct PlannedFile: Sendable {
    let sourceURL: URL
    let relativePath: String
    let sizeBytes: Int64
    let identity: SourceIdentity
  }

  private struct ImportPlan: Sendable {
    let directoryPaths: [String]
    let files: [PlannedFile]
    let totalBytes: Int64
  }

  private struct ImportSelection: Sendable {
    let containerName: String?
    let roots: [URL]
  }

  private static let maximumImportBytes: Int64 = 16 * 1_024 * 1_024 * 1_024
  private static let storageReserveBytes: Int64 = 1_024 * 1_024 * 1_024
  private static let maximumFileCount = 20_000
  private static let maximumDirectoryCount = 4_096
  private static let maximumDepth = 64
  private static let copyChunkBytes = 4 * 1_024 * 1_024
  private static var protectionAttributes: [FileAttributeKey: Any] {
    #if os(iOS)
    [.protectionKey: FileProtectionType.complete]
    #else
    [:]
    #endif
  }
  private static var protectedAtomicWriteOptions: Data.WritingOptions {
    #if os(iOS)
    [.atomic, .completeFileProtection]
    #else
    [.atomic]
    #endif
  }
  private static let coreMLSidecarNames: Set<String> = [
    "config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
    "added_tokens.json", "tokenizer.model", "merges.txt", "vocab.json"
  ]
  private static let mlxArtifactExtensions: Set<String> = [
    "json", "safetensors", "model", "txt", "jinja", "tiktoken"
  ]

  private let fileManager: FileManager
  private let rootURL: URL
  private let modelsURL: URL
  private let indexURL: URL
  private let documentsModelsURL: URL
  private var activeStagingNames: Set<String> = []
  private let importDidCreateStaging: (@Sendable () async -> Void)?
  private let promotionDidPublish: (@Sendable () throws -> Void)?

  init(
    fileManager: FileManager = .default,
    applicationSupportURL: URL? = nil,
    documentsURL: URL? = nil,
    importDidCreateStaging: (@Sendable () async -> Void)? = nil,
    promotionDidPublish: (@Sendable () throws -> Void)? = nil
  ) {
    self.fileManager = fileManager
    self.importDidCreateStaging = importDidCreateStaging
    self.promotionDidPublish = promotionDidPublish
    let applicationSupport = applicationSupportURL
      ?? fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    rootURL = applicationSupport.appendingPathComponent("SwarmerLocalInference", isDirectory: true)
    modelsURL = rootURL.appendingPathComponent("Models", isDirectory: true)
    indexURL = rootURL.appendingPathComponent("models.json", isDirectory: false)
    let documents = documentsURL
      ?? fileManager.urls(for: .documentDirectory, in: .userDomainMask)[0]
    documentsModelsURL = documents.appendingPathComponent("Models", isDirectory: true)
  }

  func list() throws -> [StoredLocalModel] {
    try prepareDirectories()
    let records: [StoredLocalModel]
    if fileManager.fileExists(atPath: indexURL.path) {
      do {
        let data = try Data(contentsOf: indexURL)
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        records = try decoder.decode([StoredLocalModel].self, from: data)
        try validateMetadata(records)
      } catch {
        throw LocalInferenceError.metadataCorrupt
      }
    } else {
      records = []
    }
    try cleanupUnindexedModels(retaining: Set(records.map(\.modelId)))
    return records.sorted { $0.importedAt > $1.importedAt }
  }

  func resolve(modelId: String) throws -> ResolvedLocalModel {
    guard let stored = try list().first(where: { $0.modelId == modelId }) else {
      throw LocalInferenceError.modelNotFound(modelId)
    }

    let modelRoot: URL
    if let origin = stored.remoteOrigin {
      modelRoot = try durableModelRoot(repositoryId: origin.repositoryId, revision: origin.revision)
      try verifyDurableModel(stored, at: modelRoot)
    } else {
      modelRoot = modelsURL.appendingPathComponent(stored.modelId, isDirectory: true)
    }
    try rejectSymbolicLinks(in: modelRoot)
    let runtimeURL = try confinedURL(relativePath: stored.runtimeRelativePath, root: modelRoot)
    guard fileManager.fileExists(atPath: runtimeURL.path) else {
      throw LocalInferenceError.sourceMissing
    }

    let tokenizerURL: URL?
    if let path = stored.tokenizerRelativePath {
      tokenizerURL = try confinedURL(relativePath: path, root: modelRoot)
      guard fileManager.fileExists(atPath: tokenizerURL!.path) else {
        throw LocalInferenceError.sourceMissing
      }
    } else {
      tokenizerURL = nil
    }
    try rejectSymbolicLinks(in: runtimeURL)
    if let tokenizerURL { try rejectSymbolicLinks(in: tokenizerURL) }

    return ResolvedLocalModel(stored: stored, runtimeURL: runtimeURL, tokenizerURL: tokenizerURL)
  }

  func purpose(for record: StoredLocalModel) throws -> LocalModelPurpose {
    guard record.runtime == .mlx else { return .generation }
    if record.purpose != nil || record.remoteOrigin?.repositoryId == EmbeddingValidation.repository {
      return record.effectivePurpose
    }
    // Legacy local imports have no immutable repository identity. Classify their
    // existing config without rewriting the private index or Documents manifest.
    let root = try record.remoteOrigin.map {
      try durableModelRoot(repositoryId: $0.repositoryId, revision: $0.revision)
    } ?? modelsURL.appendingPathComponent(record.modelId, isDirectory: true)
    try rejectSymbolicLinks(in: root)
    let directory = try confinedURL(relativePath: record.runtimeRelativePath, root: root)
    return LocalModelPurpose.mlx(configuration: try readJSONObject(directory.appendingPathComponent("config.json")))
  }

  func importModel(
    runtime: LocalRuntime,
    uri: String,
    displayName: String?
  ) async throws -> StoredLocalModel {
    try prepareDirectories()
    let existingRecords = try list()
    try Task.checkCancellation()
    guard let sourceURL = URL(string: uri), sourceURL.isFileURL else {
      throw LocalInferenceError.invalidURI
    }

    let source = sourceURL.standardizedFileURL
    let scoped = source.startAccessingSecurityScopedResource()
    defer {
      if scoped { source.stopAccessingSecurityScopedResource() }
    }
    guard fileManager.fileExists(atPath: source.path) else {
      throw LocalInferenceError.sourceMissing
    }
    let rootValues = try source.resourceValues(forKeys: [.isSymbolicLinkKey])
    guard rootValues.isSymbolicLink != true else {
      throw LocalInferenceError.symbolicLinkRejected
    }

    let selection = try importSelection(runtime: runtime, source: source)
    let plan = try buildImportPlan(selection: selection)
    try ensureAvailableStorage(for: plan.totalBytes)
    try Task.checkCancellation()

    let modelId = UUID().uuidString.lowercased()
    let stagingURL = modelsURL.appendingPathComponent(".import-\(modelId)", isDirectory: true)
    activeStagingNames.insert(stagingURL.lastPathComponent)
    defer { activeStagingNames.remove(stagingURL.lastPathComponent) }
    let destinationURL = modelsURL.appendingPathComponent(modelId, isDirectory: true)
    try fileManager.createDirectory(
      at: stagingURL,
      withIntermediateDirectories: false,
      attributes: Self.protectionAttributes
    )

    do {
      if let importDidCreateStaging {
        await importDidCreateStaging()
        try Task.checkCancellation()
      }
      let payloadURL = stagingURL.appendingPathComponent("payload", isDirectory: true)
      try fileManager.createDirectory(
        at: payloadURL,
        withIntermediateDirectories: false,
        attributes: Self.protectionAttributes
      )
      try copy(plan: plan, into: payloadURL)
      try Task.checkCancellation()
      try rejectSymbolicLinks(in: payloadURL)

      let resolved = try validate(runtime: runtime, payloadURL: payloadURL)
      let name = try validatedDisplayName(
        displayName,
        fallback: source.deletingPathExtension().lastPathComponent
      )
      let record = StoredLocalModel(
        modelId: modelId,
        runtime: runtime,
        displayName: name,
        source: source.lastPathComponent,
        sizeBytes: plan.totalBytes,
        importedAt: Date(),
        runtimeRelativePath: resolved.runtimeURL.path(relativeTo: stagingURL),
        tokenizerRelativePath: resolved.tokenizerURL?.path(relativeTo: stagingURL),
        purpose: runtime == .mlx
          ? LocalModelPurpose.mlx(configuration: try readJSONObject(resolved.runtimeURL.appendingPathComponent("config.json")))
          : .generation
      )

      try Task.checkCancellation()
      try fileManager.moveItem(at: stagingURL, to: destinationURL)
      do {
        try persist(existingRecords + [record])
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var excludedURL = destinationURL
        try? excludedURL.setResourceValues(values)
        return record
      } catch {
        try? fileManager.removeItem(at: destinationURL)
        throw error
      }
    } catch {
      try? fileManager.removeItem(at: stagingURL)
      throw error
    }
  }

  /// Finds an already materialized Hub revision without consulting the network or cache.
  func resolveRemoteMLX(repositoryId: String, revision: String) throws -> ResolvedLocalModel? {
    let destination = try durableModelRoot(repositoryId: repositoryId, revision: revision)
    let records = try list()
    if let record = records.first(where: {
      $0.remoteOrigin?.repositoryId == repositoryId && $0.remoteOrigin?.revision == revision
    }) {
      return try resolve(modelId: record.modelId)
    }
    guard fileManager.fileExists(atPath: destination.path) else { return nil }
    // A Documents manifest is user-editable: only a private pending record can
    // authorize recovery after publication interrupted the registry write.
    let pending = pendingRemoteRecordURL(repositoryId: repositoryId, revision: revision)
    guard fileManager.fileExists(atPath: pending.path) else {
      throw LocalInferenceError.unsupportedModel("the Documents model has no trusted private registry entry; import it as a local model")
    }
    let record = try readStoredRecord(at: pending)
    try validateMetadata([record])
    guard record.remoteOrigin?.repositoryId == repositoryId,
          record.remoteOrigin?.revision == revision else { throw LocalInferenceError.metadataCorrupt }
    try verifyDurableModel(record, at: destination)
    guard !records.contains(where: { $0.modelId == record.modelId }) else {
      throw LocalInferenceError.metadataCorrupt
    }
    try persist(records + [record])
    try? fileManager.removeItem(at: pending)
    return ResolvedLocalModel(
      stored: record,
      runtimeURL: destination.appendingPathComponent("payload"),
      tokenizerURL: destination.appendingPathComponent("payload")
    )
  }

  /// Materializes cache symlinks into independent, protected regular files.
  func preserveMLXSnapshot(
    at snapshot: URL,
    repositoryCacheURL: URL,
    repositoryId: String,
    revision: String
  ) async throws -> ResolvedLocalModel {
    if let existing = try resolveRemoteMLX(repositoryId: repositoryId, revision: revision) {
      return existing
    }
    let destination = try durableModelRoot(repositoryId: repositoryId, revision: revision)
    let expectedSnapshot = repositoryCacheURL.appendingPathComponent("snapshots/\(revision)")
    guard snapshot.standardizedFileURL.path == expectedSnapshot.standardizedFileURL.path else {
      throw LocalInferenceError.symbolicLinkRejected
    }
    for directory in [repositoryCacheURL, expectedSnapshot.deletingLastPathComponent(), snapshot] {
      try requirePlainDirectory(directory)
    }
    let canonicalCache = repositoryCacheURL.resolvingSymlinksInPath()
    let canonicalSnapshot = snapshot.resolvingSymlinksInPath()
    var files: [PlannedFile] = []
    var totalBytes: Int64 = 0
    let children = try fileManager.contentsOfDirectory(at: snapshot, includingPropertiesForKeys: nil)
    for child in children where Self.mlxArtifactExtensions.contains(child.pathExtension.lowercased()) {
      try Task.checkCancellation()
      let name = try validatedPathComponent(child.lastPathComponent)
      let source = child.resolvingSymlinksInPath()
      let parent = source.deletingLastPathComponent()
      guard parent.path == canonicalCache.appendingPathComponent("blobs").path || parent.path == canonicalSnapshot.path else {
        throw LocalInferenceError.symbolicLinkRejected
      }
      let identity = try sourceIdentity(for: source)
      try appendPlannedFile(
        source: source, relativePath: name, size: identity.sizeBytes,
        files: &files, totalBytes: &totalBytes
      )
    }
    let plan = ImportPlan(directoryPaths: [], files: files.sorted { $0.relativePath < $1.relativePath }, totalBytes: totalBytes)
    try validateMLXSnapshotPlan(plan)
    try ensureAvailableStorage(for: totalBytes)
    try Task.checkCancellation()

    let modelId = UUID().uuidString.lowercased()
    let staging = modelsURL.appendingPathComponent(".import-\(modelId)")
    let pending = pendingRemoteRecordURL(repositoryId: repositoryId, revision: revision)
    var published = false
    activeStagingNames.insert(staging.lastPathComponent)
    defer {
      activeStagingNames.remove(staging.lastPathComponent)
      try? fileManager.removeItem(at: staging)
      if !published { try? fileManager.removeItem(at: pending) }
    }
    try fileManager.createDirectory(at: staging, withIntermediateDirectories: false, attributes: Self.protectionAttributes)
    if let importDidCreateStaging { await importDidCreateStaging() }
    try Task.checkCancellation()
    let payload = staging.appendingPathComponent("payload")
    try fileManager.createDirectory(at: payload, withIntermediateDirectories: false, attributes: Self.protectionAttributes)
    let artifacts = try copy(plan: plan, into: payload, recordDigests: true)
    _ = try validate(runtime: .mlx, payloadURL: payload)
    let record = StoredLocalModel(
      modelId: modelId, runtime: .mlx,
      displayName: "\(repositoryId.split(separator: "/").last!) · Modèles",
      source: "Documents/Models · \(repositoryId)@\(revision)",
      sizeBytes: totalBytes, importedAt: Date(), runtimeRelativePath: "payload", tokenizerRelativePath: "payload",
      remoteOrigin: StoredRemoteModelOrigin(repositoryId: repositoryId, revision: revision, files: artifacts),
      purpose: LocalModelPurpose.mlx(configuration: try readJSONObject(payload.appendingPathComponent("config.json")))
    )
    try validateMetadata([record])
    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .iso8601
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    let encodedRecord = try encoder.encode(record)
    try encodedRecord.write(
      to: staging.appendingPathComponent("swarmer-model.json"), options: Self.protectedAtomicWriteOptions
    )
    try prepareDurableParent(repositoryId: repositoryId)
    try Task.checkCancellation()
    try encodedRecord.write(to: pending, options: Self.protectedAtomicWriteOptions)
    // Application Support and Documents are on the same app-container volume.
    try fileManager.moveItem(at: staging, to: destination)
    published = true
    try promotionDidPublish?()
    // Read the current registry after the staging await; never overwrite concurrent imports.
    // The private pending record anchors recovery if this index write is interrupted.
    try persist(try list() + [record])
    try? fileManager.removeItem(at: pending)
    var values = URLResourceValues()
    values.isExcludedFromBackup = true
    var excludedURL = destination
    try? excludedURL.setResourceValues(values)
    return ResolvedLocalModel(stored: record, runtimeURL: destination.appendingPathComponent("payload"), tokenizerURL: destination.appendingPathComponent("payload"))
  }

  private func durableModelRoot(repositoryId: String, revision: String) throws -> URL {
    guard repositoryId.range(
      of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$",
      options: .regularExpression
    ) != nil, revision.range(of: "^[a-f0-9]{40}$", options: .regularExpression) != nil else {
      throw LocalInferenceError.invalidDownloadMetadata
    }
    return documentsModelsURL.appendingPathComponent(repositoryId).appendingPathComponent(revision)
  }

  private func pendingRemoteRecordURL(repositoryId: String, revision: String) -> URL {
    let digest = SHA256.hash(data: Data("\(repositoryId)@\(revision)".utf8))
      .map { String(format: "%02x", $0) }.joined()
    return rootURL.appendingPathComponent(".pending-mlx-\(digest).json")
  }

  private func requirePlainDirectory(_ directory: URL) throws {
    let values = try directory.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
    guard values.isSymbolicLink != true else { throw LocalInferenceError.symbolicLinkRejected }
    guard values.isDirectory == true else { throw LocalInferenceError.sourceMissing }
  }

  private func prepareDurableParent(repositoryId: String) throws {
    var directory = documentsModelsURL.deletingLastPathComponent()
    try requirePlainDirectory(directory)
    for component in ["Models"] + repositoryId.split(separator: "/").map(String.init) {
      directory.appendPathComponent(component)
      if !fileManager.fileExists(atPath: directory.path) {
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: false, attributes: Self.protectionAttributes)
      }
      try requirePlainDirectory(directory)
    }
  }

  private func validateMLXSnapshotPlan(_ plan: ImportPlan) throws {
    let byName = Dictionary(uniqueKeysWithValues: plan.files.map { ($0.relativePath, $0) })
    for required in ["config.json", "tokenizer.json", "tokenizer_config.json"] {
      guard let file = byName[required] else { throw LocalInferenceError.sourceMissing }
      _ = try readJSONObject(file.sourceURL)
    }
    guard plan.files.contains(where: { $0.relativePath.hasSuffix(".safetensors") }),
          plan.files.allSatisfy({ $0.sizeBytes > 0 }) else { throw LocalInferenceError.sourceMissing }
    for index in plan.files where index.relativePath.hasSuffix(".safetensors.index.json") {
      guard let mapping = try readJSONObject(index.sourceURL)["weight_map"] as? [String: String], !mapping.isEmpty else {
        throw LocalInferenceError.unsupportedModel("the MLX weight index is invalid")
      }
      for filename in Set(mapping.values) {
        guard filename.hasSuffix(".safetensors"), byName[filename] != nil else {
          throw LocalInferenceError.sourceMissing
        }
      }
    }
  }

  private func readJSONObject(_ url: URL) throws -> [String: Any] {
    guard try sourceIdentity(for: url).sizeBytes <= 64 * 1_024 * 1_024,
          let object = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any] else {
      throw LocalInferenceError.unsupportedModel("the MLX JSON sidecar is invalid or too large")
    }
    return object
  }

  private func readDurableManifest(at directory: URL) throws -> StoredLocalModel {
    // Reject parent redirection as well as symlinks within this one model.
    var parent = directory
    while parent.path.count >= documentsModelsURL.path.count {
      try requirePlainDirectory(parent)
      if parent.path == documentsModelsURL.path { break }
      parent.deleteLastPathComponent()
    }
    return try readStoredRecord(at: directory.appendingPathComponent("swarmer-model.json"))
  }

  private func readStoredRecord(at url: URL) throws -> StoredLocalModel {
    guard try sourceIdentity(for: url).sizeBytes <= 16 * 1_024 * 1_024 else {
      throw LocalInferenceError.metadataCorrupt
    }
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    return try decoder.decode(StoredLocalModel.self, from: Data(contentsOf: url))
  }

  private func verifyDurableModel(_ record: StoredLocalModel, at directory: URL) throws {
    guard let origin = record.remoteOrigin, try readDurableManifest(at: directory) == record else {
      throw LocalInferenceError.metadataCorrupt
    }
    let payload = directory.appendingPathComponent("payload")
    try requirePlainDirectory(payload)
    let contents = try fileManager.contentsOfDirectory(at: payload, includingPropertiesForKeys: nil)
    guard Set(contents.map(\.lastPathComponent)) == Set(origin.files.map(\.filename)) else {
      throw LocalInferenceError.sourceChangedDuringImport
    }
    for artifact in origin.files {
      try Task.checkCancellation()
      let file = try confinedURL(relativePath: artifact.filename, root: payload)
      let identity = try sourceIdentity(for: file)
      guard identity.sizeBytes == artifact.sizeBytes else { throw LocalInferenceError.sourceChangedDuringImport }
      let descriptor = Darwin.open(file.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
      guard descriptor >= 0 else { throw LocalInferenceError.sourceChangedDuringImport }
      let input = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
      defer { try? input.close() }
      var information = stat()
      guard fstat(descriptor, &information) == 0, sourceIdentity(from: information) == identity else {
        throw LocalInferenceError.sourceChangedDuringImport
      }
      var hash = SHA256()
      var count: Int64 = 0
      while true {
        try Task.checkCancellation()
        // FileHandle bridges through Foundation. Drain each chunk's temporary
        // NSData before loading model weights in the same asynchronous task.
        let readChunk = try autoreleasepool {
          guard let data = try input.read(upToCount: Self.copyChunkBytes), !data.isEmpty else { return false }
          count += Int64(data.count)
          guard count <= artifact.sizeBytes else { throw LocalInferenceError.sourceChangedDuringImport }
          hash.update(data: data)
          return true
        }
        if !readChunk { break }
      }
      guard count == artifact.sizeBytes,
            hash.finalize().map({ String(format: "%02x", $0) }).joined() == artifact.sha256,
            fstat(descriptor, &information) == 0, sourceIdentity(from: information) == identity else {
        throw LocalInferenceError.sourceChangedDuringImport
      }
    }
  }

  private func prepareDirectories() throws {
    try fileManager.createDirectory(
      at: modelsURL,
      withIntermediateDirectories: true,
      attributes: Self.protectionAttributes
    )
    var values = URLResourceValues()
    values.isExcludedFromBackup = true
    var excludedURL = rootURL
    try? excludedURL.setResourceValues(values)

    for item in try fileManager.contentsOfDirectory(
      at: modelsURL,
      includingPropertiesForKeys: [.isDirectoryKey],
      options: []
    ) where isStagingDirectoryName(item.lastPathComponent)
      && !activeStagingNames.contains(item.lastPathComponent) {
      try fileManager.removeItem(at: item)
    }
  }

  private func cleanupUnindexedModels(retaining identifiers: Set<String>) throws {
    for item in try fileManager.contentsOfDirectory(
      at: modelsURL,
      includingPropertiesForKeys: [.isDirectoryKey],
      options: []
    ) {
      let name = item.lastPathComponent
      guard isCanonicalModelId(name), !identifiers.contains(name) else { continue }
      try fileManager.removeItem(at: item)
    }
  }

  private func validateMetadata(_ records: [StoredLocalModel]) throws {
    guard Set(records.map(\.modelId)).count == records.count else {
      throw LocalInferenceError.metadataCorrupt
    }
    for record in records {
      guard isCanonicalModelId(record.modelId),
            isSafeRelativePath(record.runtimeRelativePath),
            record.tokenizerRelativePath.map(isSafeRelativePath) ?? true,
            record.sizeBytes >= 0 else {
        throw LocalInferenceError.metadataCorrupt
      }
      if let origin = record.remoteOrigin {
        _ = try durableModelRoot(repositoryId: origin.repositoryId, revision: origin.revision)
        guard record.runtime == .mlx, record.runtimeRelativePath == "payload",
              record.tokenizerRelativePath == "payload", !origin.files.isEmpty,
              origin.files.count <= Self.maximumFileCount,
              Set(origin.files.map(\.filename)).count == origin.files.count else {
          throw LocalInferenceError.metadataCorrupt
        }
        var total: Int64 = 0
        for file in origin.files {
          guard file.filename == (try validatedPathComponent(file.filename)),
                file.sizeBytes > 0,
                file.sha256.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil else {
            throw LocalInferenceError.metadataCorrupt
          }
          let (sum, overflow) = total.addingReportingOverflow(file.sizeBytes)
          guard !overflow, sum <= Self.maximumImportBytes else { throw LocalInferenceError.metadataCorrupt }
          total = sum
        }
        guard total == record.sizeBytes else { throw LocalInferenceError.metadataCorrupt }
      }
    }
  }

  private func persist(_ records: [StoredLocalModel]) throws {
    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .iso8601
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    try encoder.encode(records).write(to: indexURL, options: Self.protectedAtomicWriteOptions)
  }

  private func validatedDisplayName(_ requested: String?, fallback: String) throws -> String {
    let candidate = (requested ?? fallback).trimmingCharacters(in: .whitespacesAndNewlines)
    guard !candidate.isEmpty, candidate.count <= 120,
          candidate.rangeOfCharacter(from: .controlCharacters) == nil else {
      throw LocalInferenceError.invalidDisplayName
    }
    return candidate
  }

  private func importSelection(runtime: LocalRuntime, source: URL) throws -> ImportSelection {
    let values = try source.resourceValues(
      forKeys: [.isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey]
    )
    guard values.isSymbolicLink != true else {
      throw LocalInferenceError.symbolicLinkRejected
    }

    switch runtime {
    case .llamaCpp:
      guard values.isRegularFile == true, source.pathExtension.lowercased() == "gguf" else {
        throw LocalInferenceError.unsupportedModel("select exactly one regular .gguf file")
      }
      return ImportSelection(containerName: nil, roots: [source])

    case .coreML:
      guard values.isDirectory == true else {
        throw LocalInferenceError.unsupportedModel(
          "select a folder containing one Core ML model and its tokenizer sidecars"
        )
      }
      let children = try fileManager.contentsOfDirectory(
        at: source,
        includingPropertiesForKeys: [.isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey],
        options: []
      )
      let models = children.filter {
        ["mlmodel", "mlpackage", "mlmodelc"].contains($0.pathExtension.lowercased())
      }
      guard models.count == 1 else {
        throw LocalInferenceError.ambiguousModel("the selected folder must contain exactly one Core ML model")
      }
      for required in ["tokenizer.json", "tokenizer_config.json"] {
        let url = source.appendingPathComponent(required, isDirectory: false)
        let requiredValues = try? url.resourceValues(
          forKeys: [.isRegularFileKey, .isSymbolicLinkKey]
        )
        guard requiredValues?.isRegularFile == true,
              requiredValues?.isSymbolicLink != true else {
          throw LocalInferenceError.unsupportedModel(
            "tokenizer.json and tokenizer_config.json must be direct files in the selected folder"
          )
        }
      }
      let sidecars = children.filter { Self.coreMLSidecarNames.contains($0.lastPathComponent) }
      return ImportSelection(
        containerName: try validatedPathComponent(source.lastPathComponent),
        roots: models + sidecars
      )

    case .mlx:
      guard values.isDirectory == true else {
        throw LocalInferenceError.unsupportedModel("select a complete local MLX model folder")
      }
      let children = try fileManager.contentsOfDirectory(
        at: source,
        includingPropertiesForKeys: [.isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey],
        options: []
      )
      let artifacts = try children.filter { child in
        let childValues = try child.resourceValues(
          forKeys: [.isRegularFileKey, .isSymbolicLinkKey]
        )
        guard childValues.isSymbolicLink != true else {
          throw LocalInferenceError.symbolicLinkRejected
        }
        return childValues.isRegularFile == true
          && Self.mlxArtifactExtensions.contains(child.pathExtension.lowercased())
      }
      let names = Set(artifacts.map(\.lastPathComponent))
      guard names.contains("config.json"), names.contains("tokenizer.json"),
            artifacts.contains(where: { $0.pathExtension.lowercased() == "safetensors" }) else {
        throw LocalInferenceError.unsupportedModel(
          "config.json, tokenizer.json, and safetensors weights must be direct files in the selected folder"
        )
      }
      return ImportSelection(
        containerName: try validatedPathComponent(source.lastPathComponent),
        roots: artifacts
      )
    }
  }

  private func buildImportPlan(selection: ImportSelection) throws -> ImportPlan {
    var directories: [String] = []
    var files: [PlannedFile] = []
    var totalBytes: Int64 = 0
    if let containerName = selection.containerName {
      directories.append(containerName)
    }

    for root in selection.roots {
      try Task.checkCancellation()
      let rootName = try validatedPathComponent(root.lastPathComponent)
      let relativeRoot = selection.containerName.map { "\($0)/\(rootName)" } ?? rootName
      let rootValues = try root.resourceValues(
        forKeys: [.isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]
      )
      guard rootValues.isSymbolicLink != true else {
        throw LocalInferenceError.symbolicLinkRejected
      }
      if rootValues.isRegularFile == true {
        try appendPlannedFile(
          source: root,
          relativePath: relativeRoot,
          size: Int64(rootValues.fileSize ?? 0),
          files: &files,
          totalBytes: &totalBytes
        )
        continue
      }
      guard rootValues.isDirectory == true else {
        throw LocalInferenceError.unsupportedModel("model bundles may contain only directories and regular files")
      }
      directories.append(relativeRoot)
      var pending: [(url: URL, relativePath: String, depth: Int)] = [(root, relativeRoot, 1)]
      while let directory = pending.popLast() {
        try Task.checkCancellation()
        guard directory.depth <= Self.maximumDepth else {
          throw LocalInferenceError.importHasTooManyFiles
        }
        let children = try fileManager.contentsOfDirectory(
          at: directory.url,
          includingPropertiesForKeys: [
            .isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey
          ],
          options: []
        )
        for child in children {
          try Task.checkCancellation()
          let name = try validatedPathComponent(child.lastPathComponent)
          let relativePath = "\(directory.relativePath)/\(name)"
          guard relativePath.utf8.count <= 4_096 else {
            throw LocalInferenceError.importHasTooManyFiles
          }
          let childValues = try child.resourceValues(
            forKeys: [.isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]
          )
          guard childValues.isSymbolicLink != true else {
            throw LocalInferenceError.symbolicLinkRejected
          }
          if childValues.isDirectory == true {
            directories.append(relativePath)
            guard directories.count <= Self.maximumDirectoryCount else {
              throw LocalInferenceError.importHasTooManyFiles
            }
            pending.append((child, relativePath, directory.depth + 1))
          } else if childValues.isRegularFile == true {
            try appendPlannedFile(
              source: child,
              relativePath: relativePath,
              size: Int64(childValues.fileSize ?? 0),
              files: &files,
              totalBytes: &totalBytes
            )
          } else {
            throw LocalInferenceError.unsupportedModel(
              "model bundles may contain only directories and regular files"
            )
          }
        }
      }
    }

    guard !files.isEmpty else {
      throw LocalInferenceError.unsupportedModel("the selected model contains no regular files")
    }
    return ImportPlan(
      directoryPaths: Array(Set(directories)).sorted {
        let leftDepth = $0.split(separator: "/").count
        let rightDepth = $1.split(separator: "/").count
        return leftDepth == rightDepth ? $0 < $1 : leftDepth < rightDepth
      },
      files: files.sorted { $0.relativePath < $1.relativePath },
      totalBytes: totalBytes
    )
  }

  private func appendPlannedFile(
    source: URL,
    relativePath: String,
    size: Int64,
    files: inout [PlannedFile],
    totalBytes: inout Int64
  ) throws {
    let identity = try sourceIdentity(for: source)
    guard size >= 0, identity.sizeBytes == size else {
      throw LocalInferenceError.sourceChangedDuringImport
    }
    guard files.count < Self.maximumFileCount else {
      throw LocalInferenceError.importHasTooManyFiles
    }
    let (sum, overflow) = totalBytes.addingReportingOverflow(size)
    guard !overflow, sum <= Self.maximumImportBytes else {
      throw LocalInferenceError.importTooLarge
    }
    files.append(
      PlannedFile(
        sourceURL: source,
        relativePath: relativePath,
        sizeBytes: size,
        identity: identity
      )
    )
    totalBytes = sum
  }

  private func ensureAvailableStorage(for bytes: Int64) throws {
    let capacity = try modelsURL.resourceValues(
      forKeys: [.volumeAvailableCapacityForImportantUsageKey]
    ).volumeAvailableCapacityForImportantUsage
    let (required, overflow) = bytes.addingReportingOverflow(Self.storageReserveBytes)
    guard !overflow, let capacity, capacity >= required else {
      throw LocalInferenceError.insufficientStorage
    }
  }

  @discardableResult
  private func copy(plan: ImportPlan, into payload: URL, recordDigests: Bool = false) throws -> [StoredModelArtifact] {
    var artifacts: [StoredModelArtifact] = []
    for relativePath in plan.directoryPaths {
      try Task.checkCancellation()
      let destination = try confinedURL(relativePath: relativePath, root: payload)
      try fileManager.createDirectory(
        at: destination,
        withIntermediateDirectories: false,
        attributes: Self.protectionAttributes
      )
    }

    var totalCopied: Int64 = 0
    for file in plan.files {
      try Task.checkCancellation()
      let destination = try confinedURL(relativePath: file.relativePath, root: payload)
      guard fileManager.createFile(
        atPath: destination.path,
        contents: nil,
        attributes: Self.protectionAttributes
      ) else {
        throw LocalInferenceError.inferenceFailed("the private model file could not be created")
      }
      let descriptor = Darwin.open(file.sourceURL.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
      guard descriptor >= 0 else {
        if errno == ELOOP { throw LocalInferenceError.symbolicLinkRejected }
        throw LocalInferenceError.sourceChangedDuringImport
      }
      var openedStat = stat()
      guard fstat(descriptor, &openedStat) == 0,
            sourceIdentity(from: openedStat) == file.identity else {
        Darwin.close(descriptor)
        throw LocalInferenceError.sourceChangedDuringImport
      }
      let input = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
      let output = try FileHandle(forWritingTo: destination)
      defer {
        try? input.close()
        try? output.close()
      }
      var fileCopied: Int64 = 0
      var digest = SHA256()
      while true {
        try Task.checkCancellation()
        let readChunk = try autoreleasepool {
          guard let data = try input.read(upToCount: Self.copyChunkBytes), !data.isEmpty else { return false }
          let (nextFileSize, fileOverflow) = fileCopied.addingReportingOverflow(Int64(data.count))
          let (nextTotal, totalOverflow) = totalCopied.addingReportingOverflow(Int64(data.count))
          guard !fileOverflow, !totalOverflow,
                nextFileSize <= file.sizeBytes,
                nextTotal <= plan.totalBytes else {
            throw LocalInferenceError.sourceChangedDuringImport
          }
          try output.write(contentsOf: data)
          if recordDigests { digest.update(data: data) }
          fileCopied = nextFileSize
          totalCopied = nextTotal
          return true
        }
        if !readChunk { break }
      }
      guard fileCopied == file.sizeBytes else {
        throw LocalInferenceError.sourceChangedDuringImport
      }
      try output.synchronize()
      var completedStat = stat()
      guard fstat(input.fileDescriptor, &completedStat) == 0,
            sourceIdentity(from: completedStat) == file.identity else {
        throw LocalInferenceError.sourceChangedDuringImport
      }
      if recordDigests {
        artifacts.append(StoredModelArtifact(
          filename: file.relativePath,
          sizeBytes: fileCopied,
          sha256: digest.finalize().map { String(format: "%02x", $0) }.joined()
        ))
      }
    }
    guard totalCopied == plan.totalBytes else {
      throw LocalInferenceError.sourceChangedDuringImport
    }
    return artifacts
  }

  private func sourceIdentity(for url: URL) throws -> SourceIdentity {
    var information = stat()
    guard lstat(url.path, &information) == 0 else {
      throw LocalInferenceError.sourceChangedDuringImport
    }
    guard information.st_mode & S_IFMT == S_IFREG else {
      if information.st_mode & S_IFMT == S_IFLNK {
        throw LocalInferenceError.symbolicLinkRejected
      }
      throw LocalInferenceError.unsupportedModel("model artifacts must be regular files")
    }
    return sourceIdentity(from: information)
  }

  private func sourceIdentity(from information: stat) -> SourceIdentity {
    SourceIdentity(
      device: UInt64(information.st_dev),
      inode: UInt64(information.st_ino),
      sizeBytes: Int64(information.st_size),
      modifiedSeconds: Int64(information.st_mtimespec.tv_sec),
      modifiedNanoseconds: Int64(information.st_mtimespec.tv_nsec),
      changedSeconds: Int64(information.st_ctimespec.tv_sec),
      changedNanoseconds: Int64(information.st_ctimespec.tv_nsec)
    )
  }

  private func rejectSymbolicLinks(in root: URL) throws {
    let rootValues = try root.resourceValues(forKeys: [.isSymbolicLinkKey, .isDirectoryKey])
    if rootValues.isSymbolicLink == true { throw LocalInferenceError.symbolicLinkRejected }
    guard rootValues.isDirectory == true else { return }

    guard let enumerator = fileManager.enumerator(
      at: root,
      includingPropertiesForKeys: [.isSymbolicLinkKey],
      options: []
    ) else {
      throw LocalInferenceError.sourceMissing
    }
    for case let item as URL in enumerator {
      if try item.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink == true {
        throw LocalInferenceError.symbolicLinkRejected
      }
    }
  }

  private func validate(
    runtime: LocalRuntime,
    payloadURL: URL
  ) throws -> (runtimeURL: URL, tokenizerURL: URL?) {
    switch runtime {
    case .llamaCpp:
      let models = try files(in: payloadURL, extensions: ["gguf"])
      guard !models.isEmpty else {
        throw LocalInferenceError.unsupportedModel("a .gguf file is required")
      }
      guard models.count == 1 else {
        throw LocalInferenceError.ambiguousModel("exactly one .gguf file is required")
      }
      return (models[0], nil)

    case .coreML:
      let models = try files(in: payloadURL, extensions: ["mlmodel", "mlpackage", "mlmodelc"])
      guard !models.isEmpty else {
        throw LocalInferenceError.unsupportedModel("a .mlmodel, .mlpackage, or .mlmodelc model is required")
      }
      guard models.count == 1 else {
        throw LocalInferenceError.ambiguousModel("exactly one Core ML model is required")
      }
      guard let tokenizer = tokenizerDirectory(
        startingAt: models[0].deletingLastPathComponent(),
        within: payloadURL
      ) else {
        throw LocalInferenceError.unsupportedModel(
          "tokenizer.json and tokenizer_config.json sidecars are required"
        )
      }
      return (models[0], tokenizer)

    case .mlx:
      let directories = try directories(including: payloadURL)
      let matches = try directories.filter { directory in
        let config = directory.appendingPathComponent("config.json")
        let tokenizer = directory.appendingPathComponent("tokenizer.json")
        guard fileManager.fileExists(atPath: config.path),
              fileManager.fileExists(atPath: tokenizer.path) else {
          return false
        }
        return try fileManager.contentsOfDirectory(
          at: directory,
          includingPropertiesForKeys: nil
        ).contains { $0.pathExtension.lowercased() == "safetensors" }
      }
      guard !matches.isEmpty else {
        throw LocalInferenceError.unsupportedModel(
          "an MLX directory with config, tokenizer, and safetensors files is required"
        )
      }
      guard matches.count == 1 else {
        throw LocalInferenceError.ambiguousModel("exactly one MLX model directory is required")
      }
      return (matches[0], matches[0])
    }
  }

  private func files(in root: URL, extensions allowed: Set<String>) throws -> [URL] {
    guard let enumerator = fileManager.enumerator(
      at: root,
      includingPropertiesForKeys: [.isRegularFileKey, .isDirectoryKey],
      options: []
    ) else { return [] }

    var matches: [URL] = []
    for case let item as URL in enumerator {
      let ext = item.pathExtension.lowercased()
      guard allowed.contains(ext) else { continue }
      matches.append(item)
      if ["mlpackage", "mlmodelc"].contains(ext) {
        enumerator.skipDescendants()
      }
    }
    return matches.sorted { $0.path < $1.path }
  }

  private func directories(including root: URL) throws -> [URL] {
    var result = [root]
    guard let enumerator = fileManager.enumerator(
      at: root,
      includingPropertiesForKeys: [.isDirectoryKey],
      options: []
    ) else { return result }
    for case let item as URL in enumerator {
      if try item.resourceValues(forKeys: [.isDirectoryKey]).isDirectory == true {
        result.append(item)
      }
    }
    return result
  }

  private func tokenizerDirectory(startingAt start: URL, within root: URL) -> URL? {
    var candidate = start.standardizedFileURL
    let boundary = root.standardizedFileURL.path
    while candidate.path.hasPrefix(boundary) {
      let tokenizer = candidate.appendingPathComponent("tokenizer.json")
      let config = candidate.appendingPathComponent("tokenizer_config.json")
      if fileManager.fileExists(atPath: tokenizer.path),
         fileManager.fileExists(atPath: config.path) {
        return candidate
      }
      if candidate.path == boundary { break }
      candidate.deleteLastPathComponent()
    }
    return nil
  }

  private func validatedPathComponent(_ value: String) throws -> String {
    guard !value.isEmpty, value != ".", value != "..", !value.contains("/"),
          value.rangeOfCharacter(from: .controlCharacters) == nil else {
      throw LocalInferenceError.unsupportedModel("the model contains an invalid file name")
    }
    return value
  }

  private func isSafeRelativePath(_ value: String) -> Bool {
    !value.isEmpty && !value.hasPrefix("/")
      && value.split(separator: "/", omittingEmptySubsequences: false).allSatisfy {
        !$0.isEmpty && $0 != "." && $0 != ".."
      }
  }

  private func isCanonicalModelId(_ value: String) -> Bool {
    guard let uuid = UUID(uuidString: value) else { return false }
    return uuid.uuidString.lowercased() == value
  }

  private func isStagingDirectoryName(_ value: String) -> Bool {
    let prefix = ".import-"
    guard value.hasPrefix(prefix) else { return false }
    return isCanonicalModelId(String(value.dropFirst(prefix.count)))
  }

  private func confinedURL(relativePath: String, root: URL) throws -> URL {
    guard isSafeRelativePath(relativePath) else {
      throw LocalInferenceError.metadataCorrupt
    }
    let resolved = root.appendingPathComponent(relativePath).standardizedFileURL
    let boundary = root.standardizedFileURL.path + "/"
    guard resolved.path.hasPrefix(boundary) else { throw LocalInferenceError.metadataCorrupt }
    return resolved
  }
}

private extension URL {
  func path(relativeTo base: URL) -> String {
    let basePath = base.standardizedFileURL.path
    let value = standardizedFileURL.path
    guard value.hasPrefix(basePath + "/") else { return lastPathComponent }
    return String(value.dropFirst(basePath.count + 1))
  }
}
