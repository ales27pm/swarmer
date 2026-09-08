import Foundation
import Darwin

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
    "config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
    "added_tokens.json", "tokenizer.model", "merges.txt", "vocab.json"
  ]
  private static let mlxArtifactExtensions: Set<String> = [
    "json", "safetensors", "model", "txt", "jinja", "tiktoken"
  ]

  private let fileManager: FileManager
  private let rootURL: URL
  private let modelsURL: URL
  private let indexURL: URL

  init(fileManager: FileManager = .default, applicationSupportURL: URL? = nil) {
    self.fileManager = fileManager
    let applicationSupport = applicationSupportURL
      ?? fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    rootURL = applicationSupport.appendingPathComponent("SwarmerLocalInference", isDirectory: true)
    modelsURL = rootURL.appendingPathComponent("Models", isDirectory: true)
    indexURL = rootURL.appendingPathComponent("models.json", isDirectory: false)
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

    let modelRoot = modelsURL.appendingPathComponent(stored.modelId, isDirectory: true)
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
    let destinationURL = modelsURL.appendingPathComponent(modelId, isDirectory: true)
    try fileManager.createDirectory(
      at: stagingURL,
      withIntermediateDirectories: false,
      attributes: Self.protectionAttributes
    )

    do {
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
        tokenizerRelativePath: resolved.tokenizerURL?.path(relativeTo: stagingURL)
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
    ) where isStagingDirectoryName(item.lastPathComponent) {
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

  private func copy(plan: ImportPlan, into payload: URL) throws {
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
      while true {
        try Task.checkCancellation()
        guard let data = try input.read(upToCount: Self.copyChunkBytes), !data.isEmpty else { break }
        let (nextFileSize, fileOverflow) = fileCopied.addingReportingOverflow(Int64(data.count))
        let (nextTotal, totalOverflow) = totalCopied.addingReportingOverflow(Int64(data.count))
        guard !fileOverflow, !totalOverflow,
              nextFileSize <= file.sizeBytes,
              nextTotal <= plan.totalBytes else {
          throw LocalInferenceError.sourceChangedDuringImport
        }
        try output.write(contentsOf: data)
        fileCopied = nextFileSize
        totalCopied = nextTotal
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
    }
    guard totalCopied == plan.totalBytes else {
      throw LocalInferenceError.sourceChangedDuringImport
    }
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
