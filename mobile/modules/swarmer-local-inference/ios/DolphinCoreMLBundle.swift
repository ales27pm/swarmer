import Foundation

enum DolphinCoreMLBundleError: LocalizedError, Sendable {
  case invalid(String)

  var errorDescription: String? {
    switch self {
    case .invalid(let detail):
      return detail
    }
  }
}

struct DolphinCoreMLBundle: Sendable {
  struct Manifest: Decodable, Sendable {
    let formatVersion: Int
    let sourceModel: String?
    let sourceRevision: String?
    let sourceWeightIdentity: SourceWeightIdentity?
    let contextLength: Int
    let vocabSize: Int
    let hiddenSize: Int
    let headDimension: Int
    let eosTokenIds: [Int32]
    let headSizes: [Int]
    let blockNames: [String]
    let modelFiles: [String]
    let stateBytes: Int

    private enum CodingKeys: String, CodingKey {
      case formatVersion = "format_version"
      case sourceModel = "source_model"
      case sourceRevision = "source_revision"
      case sourceWeightIdentity = "source_weight_identity"
      case contextLength = "context_length"
      case vocabSize = "vocab_size"
      case hiddenSize = "hidden_size"
      case headDimension = "head_dim"
      case eosTokenIds = "eos_token_ids"
      case headSizes = "head_sizes"
      case blockNames = "block_names"
      case modelFiles = "model_files"
      case stateBytes = "state_bytes"
    }
  }

  // This checks the declared source contract only. It does not authenticate the
  // converted package bytes or establish device qualification. Schema 3 release
  // verification remains the export toolkit's responsibility.
  struct SourceWeightIdentity: Codable, Equatable, Sendable {
    struct FileRecord: Codable, Equatable, Sendable {
      let file: String
      let bytes: Int64
      let sha256: String
    }
    let repo: String
    let revision: String
    let shards: [FileRecord]
    let metadata: [FileRecord]
    let identitySHA256: String

    private enum CodingKeys: String, CodingKey {
      case repo, revision, shards, metadata
      case identitySHA256 = "identity_sha256"
    }
  }

  // Pinned by CoreML-DOLPHIN3.0 download_source.py at 6eb4c21.
  static let expectedSourceIdentity = SourceWeightIdentity(
    repo: "dphn/Dolphin3.0-Llama3.2-3B",
    revision: "392a6f57223e7ccfe6ef4ebdb2ff101a42d57364",
    shards: [
      .init(file: "model-00001-of-00002.safetensors", bytes: 4965811384,
            sha256: "30660d7b8dc6288690933fcad0609d250bd7cd8d88e633e5886c7f5a500236f2"),
      .init(file: "model-00002-of-00002.safetensors", bytes: 1459729952,
            sha256: "ea93d07c32c36d1385491bf45f13188e1e08647c066de01e33fc20a262448bf5")
    ],
    metadata: [
      .init(file: "README.md", bytes: 5840,
            sha256: "0c4007169c0ebeb8928283543a8f5115ef85414f437aa7cfa5ad2b85095c3336"),
      .init(file: "config.json", bytes: 935,
            sha256: "e21ff53ea39726f972362beba869807216775d5e308bc2f531784846c06a0249"),
      .init(file: "generation_config.json", bytes: 206,
            sha256: "e627b5a8b2dc371f90388947ada64fa6e71de0f991c04c835f0c0bc97e305a4f"),
      .init(file: "model.safetensors.index.json", bytes: 20919,
            sha256: "e6322ad005d27134ba439f7ddd572e632e5b2116ebfbdc88186596dada904c75"),
      .init(file: "special_tokens_map.json", bytes: 444,
            sha256: "2df2c4620bb1a9eb877bc7c90c7fa04608bda9fa7c0cf2cdcc0a17b849649683"),
      .init(file: "tokenizer.json", bytes: 17210298,
            sha256: "e40b93124a3e29f62d5f4ff41be56cb2af34ecacf9239acd9da53a98860380b5"),
      .init(file: "tokenizer_config.json", bytes: 53573,
            sha256: "51ad9580aba8d00016efda43357185a0d8ff9884584dcc82ab58ca552afd14e1")
    ],
    identitySHA256: "c8deb65551392cfd3f87113e0a0cf3486af96c18e551e5aa7d219d90c8c6d5b1"
  )

  static let manifestName = "manifest.json"
  static let ropeNames = ["rope_cos.fp16", "rope_sin.fp16"]
  static let requiredTokenizerNames = ["tokenizer.json", "tokenizer_config.json"]
  static let expectedBlockNames = (0..<7).map { String(format: "block_%02d", $0) }
  static let expectedModelStems = ["embedding"] + expectedBlockNames + ["head"]
  static let expectedModelFiles = expectedModelStems.map { "\($0).mlpackage" }
  static let expectedHeadSizes = Array(repeating: 16_032, count: 7) + [16_034]
  static let expectedEOSTokenIds: [Int32] = [128_256, 128_001, 128_008, 128_009]
  static let expectedRoPEBytes = 2_048 * 128 * MemoryLayout<UInt16>.size
  static let maximumManifestBytes = 1_048_576
  static let selectableSidecarNames: Set<String> = [
    manifestName, "rope_cos.fp16", "rope_sin.fp16", "config.json",
    "generation_config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "added_tokens.json", "tokenizer.model",
    "merges.txt", "vocab.json"
  ]

  let directory: URL
  let manifest: Manifest
  let modelURLs: [URL]
  let sidecarURLs: [URL]

  static func isMarked(at directory: URL, fileManager: FileManager = .default) -> Bool {
    guard let names = try? fileManager.contentsOfDirectory(atPath: directory.path) else {
      return false
    }
    // Core ML packages contain `Manifest.json`. Match the Dolphin marker with
    // exact casing so case-insensitive Apple filesystems do not confuse the two.
    let nameSet = Set(names)
    let modelSentinels = Set(
      expectedModelStems.flatMap { ["\($0).mlpackage", "\($0).mlmodelc"] }
    )
    return nameSet.contains(manifestName)
      && Set(ropeNames).isSubset(of: nameSet)
      && !modelSentinels.isDisjoint(with: nameSet)
  }

  static func inspect(
    directory: URL,
    fileManager: FileManager = .default
  ) throws -> DolphinCoreMLBundle {
    let root = directory.standardizedFileURL
    let rootValues = try root.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
    guard rootValues.isDirectory == true, rootValues.isSymbolicLink != true else {
      throw DolphinCoreMLBundleError.invalid("the Dolphin bundle root must be a regular directory")
    }

    let manifestURL = root.appendingPathComponent(manifestName, isDirectory: false)
    try requireRegularFile(manifestURL, fileManager: fileManager)
    let manifest: Manifest
    do {
      let handle = try FileHandle(forReadingFrom: manifestURL)
      defer { try? handle.close() }
      let data = try handle.read(upToCount: maximumManifestBytes + 1) ?? Data()
      guard !data.isEmpty, data.count <= maximumManifestBytes else {
        throw DolphinCoreMLBundleError.invalid("manifest.json exceeds the 1 MiB safety limit")
      }
      manifest = try JSONDecoder().decode(Manifest.self, from: data)
    } catch let error as DolphinCoreMLBundleError {
      throw error
    } catch {
      throw DolphinCoreMLBundleError.invalid("manifest.json does not match the Dolphin v2/v3 contract")
    }
    try validate(manifest: manifest)

    let children = try fileManager.contentsOfDirectory(
      at: root,
      includingPropertiesForKeys: [.isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey],
      options: []
    )
    let childrenByName = Dictionary(grouping: children, by: \.lastPathComponent)

    var modelURLs: [URL] = []
    for stem in expectedModelStems {
      let candidates = ["\(stem).mlpackage", "\(stem).mlmodelc"].compactMap {
        childrenByName[$0]?.first
      }
      guard candidates.count == 1, let modelURL = candidates.first else {
        throw DolphinCoreMLBundleError.invalid(
          "exactly one .mlpackage or .mlmodelc is required for \(stem)"
        )
      }
      let values = try modelURL.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
      guard values.isDirectory == true, values.isSymbolicLink != true else {
        throw DolphinCoreMLBundleError.invalid("\(modelURL.lastPathComponent) must be a model directory")
      }
      modelURLs.append(modelURL)
    }

    let allowedModelNames = Set(expectedModelStems.flatMap {
      ["\($0).mlpackage", "\($0).mlmodelc"]
    })
    let unexpectedModels = children.filter {
      ["mlmodel", "mlpackage", "mlmodelc"].contains($0.pathExtension.lowercased())
        && !allowedModelNames.contains($0.lastPathComponent)
    }
    guard unexpectedModels.isEmpty else {
      throw DolphinCoreMLBundleError.invalid("the Dolphin bundle contains unexpected Core ML models")
    }

    for name in requiredTokenizerNames {
      try requireRegularFile(root.appendingPathComponent(name), fileManager: fileManager)
    }
    for name in ropeNames {
      let url = root.appendingPathComponent(name, isDirectory: false)
      try requireRegularFile(url, fileManager: fileManager)
      let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize
      guard size == expectedRoPEBytes else {
        throw DolphinCoreMLBundleError.invalid("\(name) has the wrong byte length")
      }
    }

    let sidecars = children.filter { selectableSidecarNames.contains($0.lastPathComponent) }
    for sidecar in sidecars {
      try requireRegularFile(sidecar, fileManager: fileManager)
    }
    return DolphinCoreMLBundle(
      directory: root,
      manifest: manifest,
      modelURLs: modelURLs,
      sidecarURLs: sidecars.sorted { $0.lastPathComponent < $1.lastPathComponent }
    )
  }

  static func sample(
    logits: [Float],
    temperature: Double,
    randomUnit: Double? = nil
  ) throws -> Int32 {
    guard !logits.isEmpty, logits.count <= Int(Int32.max),
          logits.allSatisfy(\.isFinite), temperature.isFinite, temperature >= 0 else {
      throw DolphinCoreMLBundleError.invalid("the Dolphin sampling inputs are invalid")
    }
    if temperature <= 0.0001 {
      let index = logits.indices.max { logits[$0] < logits[$1] } ?? 0
      return Int32(index)
    }

    let maximum = logits.max() ?? 0
    let weights = logits.map { exp(Double($0 - maximum) / temperature) }
    let total = weights.reduce(0, +)
    guard total.isFinite, total > 0 else {
      throw DolphinCoreMLBundleError.invalid("the Dolphin sampling distribution is invalid")
    }
    let unit = randomUnit ?? Double.random(in: 0..<1)
    guard unit.isFinite, unit >= 0, unit < 1 else {
      throw DolphinCoreMLBundleError.invalid("the Dolphin sampling random value is invalid")
    }
    var threshold = unit * total
    for (index, weight) in weights.enumerated() {
      if threshold < weight { return Int32(index) }
      threshold -= weight
    }
    return Int32(weights.count - 1)
  }

  private static func validate(manifest: Manifest) throws {
    if manifest.formatVersion == 3 {
      guard manifest.sourceModel == expectedSourceIdentity.repo,
            manifest.sourceRevision == expectedSourceIdentity.revision,
            manifest.sourceWeightIdentity == expectedSourceIdentity else {
        throw DolphinCoreMLBundleError.invalid("manifest.json has an unsupported Dolphin source identity")
      }
    }
    guard [2, 3].contains(manifest.formatVersion),
          manifest.contextLength == 2_048,
          manifest.vocabSize == 128_258,
          manifest.hiddenSize == 3_072,
          manifest.headDimension == 128,
          manifest.eosTokenIds == expectedEOSTokenIds,
          manifest.headSizes == expectedHeadSizes,
          manifest.blockNames == expectedBlockNames,
          manifest.modelFiles == expectedModelFiles,
          manifest.stateBytes == 234_881_024 else {
      throw DolphinCoreMLBundleError.invalid("manifest.json describes an unsupported Dolphin pipeline")
    }
  }

  private static func requireRegularFile(_ url: URL, fileManager: FileManager) throws {
    guard fileManager.fileExists(atPath: url.path) else {
      throw DolphinCoreMLBundleError.invalid("the Dolphin bundle is missing \(url.lastPathComponent)")
    }
    let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
    guard values.isRegularFile == true, values.isSymbolicLink != true else {
      throw DolphinCoreMLBundleError.invalid("\(url.lastPathComponent) must be a regular file")
    }
  }
}
