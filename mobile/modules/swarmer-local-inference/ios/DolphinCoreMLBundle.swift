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
      throw DolphinCoreMLBundleError.invalid("manifest.json does not match the Dolphin v2 contract")
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
      threshold -= weight
      if threshold <= 0 { return Int32(index) }
    }
    return Int32(weights.count - 1)
  }

  private static func validate(manifest: Manifest) throws {
    guard manifest.formatVersion == 2,
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
