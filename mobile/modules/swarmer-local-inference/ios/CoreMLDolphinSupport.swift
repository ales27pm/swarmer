import CryptoKit
import Foundation

/// The published Dolphin stateful INT4 package has a 512-token query window and
/// a separate 2,048-token KV cache. These limits must not be conflated.
enum CoreMLDolphinSupport {
  struct ArtifactFile: Equatable, Sendable {
    let path: String
    let bytes: Int64
    let sha256: String
  }

  static let officialRepository = "ales27pm/Dolphin3.0-CoreML"
  static let officialRepositoryRevision = "4972a699bb5e9e72ecb054a9d0435e9f36f959e1"
  static let officialPackageName = "Dolphin3.0-Llama3.2-3B-stateful-int4.mlpackage"
  static let contextLength = 2048
  static let queryLength = 512
  static let vocabularySize = 128258
  static let cacheShape = [28, 1, 8, contextLength, 128]
  static let stopTokenIDs: Set<Int> = [128256, 128001, 128008, 128009]

  static let officialPackageFiles = [
    ArtifactFile(
      path: "Data/com.apple.CoreML/model.mlmodel", bytes: 809_496,
      sha256: "a34a00a253c98153cf3b231105493edddde532086e224443e40b255b0f10a924"
    ),
    ArtifactFile(
      path: "Data/com.apple.CoreML/weights/weight.bin", bytes: 1_807_737_412,
      sha256: "6240edc377b1a0158812454c4bb6e3053d8e8a75a7eedb751b9740fffdfd3e15"
    ),
    ArtifactFile(
      path: "Manifest.json", bytes: 617,
      sha256: "5b8ac347a822f02ba3a6d9ccff60dd723f2649424c8e88570961f12b1c59afb6"
    ),
  ]

  static let officialSidecarFiles = [
    ArtifactFile(
      path: "config.json", bytes: 935,
      sha256: "e21ff53ea39726f972362beba869807216775d5e308bc2f531784846c06a0249"
    ),
    ArtifactFile(
      path: "generation_config.json", bytes: 206,
      sha256: "e627b5a8b2dc371f90388947ada64fa6e71de0f991c04c835f0c0bc97e305a4f"
    ),
    ArtifactFile(
      path: "special_tokens_map.json", bytes: 444,
      sha256: "2df2c4620bb1a9eb877bc7c90c7fa04608bda9fa7c0cf2cdcc0a17b849649683"
    ),
    ArtifactFile(
      path: "tokenizer.json", bytes: 17_210_298,
      sha256: "e40b93124a3e29f62d5f4ff41be56cb2af34ecacf9239acd9da53a98860380b5"
    ),
    ArtifactFile(
      path: "tokenizer_config.json", bytes: 53_573,
      sha256: "51ad9580aba8d00016efda43357185a0d8ff9884584dcc82ab58ca552afd14e1"
    ),
  ]

  enum InvalidInput: Error, LocalizedError {
    case contextExceeded
    case maskDimensions
    case stopTokenIDs
    case artifactIdentity

    var errorDescription: String? {
      switch self {
      case .contextExceeded: "Dolphin Core ML context limit exceeded."
      case .maskDimensions: "Dolphin Core ML causal-mask dimensions are invalid."
      case .stopTokenIDs: "Dolphin Core ML stop-token IDs do not match the pinned contract."
      case .artifactIdentity: "Dolphin Core ML artifact identity does not match the pinned release."
      }
    }
  }

  /// Recognize the published package by its immutable byte inventory, not by a
  /// user-controlled display name. A same-sized mutation is rejected before
  /// Core ML compilation. Compatible custom exports remain generic Core ML and
  /// are not allowed to claim the pinned Dolphin contract.
  static func validateOfficialArtifactIfCandidate(
    modelURL: URL,
    tokenizerURL: URL
  ) throws -> Bool {
    guard isOfficialArtifactCandidate(modelURL: modelURL) else { return false }
    try verifyFileInventory(root: modelURL, expected: officialPackageFiles)
    try verifyFileInventory(root: tokenizerURL, expected: officialSidecarFiles)
    return true
  }

  /// Cheap, conservative classification for UI qualification limits only.
  /// This is NOT identity verification; loading still hashes all pinned files.
  static func isOfficialArtifactCandidate(modelURL: URL) -> Bool {
    let looksOfficial = modelURL.lastPathComponent == officialPackageName
      || quickInventoryMatch(
        root: modelURL,
        expected: [officialPackageFiles[0], officialPackageFiles[2]]
      )
    return modelURL.pathExtension.lowercased() == "mlpackage" && looksOfficial
  }

  static func verifyFileInventory(root: URL, expected: [ArtifactFile]) throws {
    do {
      try verifyFileInventoryUnchecked(root: root, expected: expected)
    } catch let error as CancellationError {
      throw error
    } catch let error as InvalidInput {
      throw error
    } catch {
      throw InvalidInput.artifactIdentity
    }
  }

  private static func verifyFileInventoryUnchecked(root: URL, expected: [ArtifactFile]) throws {
    let rootValues = try root.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
    guard rootValues.isDirectory == true, rootValues.isSymbolicLink != true else {
      throw InvalidInput.artifactIdentity
    }
    for file in expected {
      let components = file.path.split(separator: "/", omittingEmptySubsequences: false)
      guard !components.isEmpty,
            components.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else {
        throw InvalidInput.artifactIdentity
      }
      var candidate = root
      for component in components {
        candidate.appendPathComponent(String(component), isDirectory: false)
        let values = try candidate.resourceValues(forKeys: [
          .fileSizeKey, .isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey,
        ])
        guard values.isSymbolicLink != true else { throw InvalidInput.artifactIdentity }
      }
      let values = try candidate.resourceValues(forKeys: [
        .fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey,
      ])
      guard values.isRegularFile == true, values.isSymbolicLink != true,
            Int64(values.fileSize ?? -1) == file.bytes,
            try sha256(candidate) == file.sha256 else {
        throw InvalidInput.artifactIdentity
      }
    }
  }

  private static func quickInventoryMatch(root: URL, expected: [ArtifactFile]) -> Bool {
    guard let values = try? root.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey]),
          values.isDirectory == true, values.isSymbolicLink != true else { return false }
    return expected.allSatisfy { file in
      let url = root.appendingPathComponent(file.path, isDirectory: false)
      guard let values = try? url.resourceValues(forKeys: [
        .fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey,
      ]) else { return false }
      return values.isRegularFile == true && values.isSymbolicLink != true
        && Int64(values.fileSize ?? -1) == file.bytes
    }
  }

  private static func sha256(_ url: URL) throws -> String {
    try Task.checkCancellation()
    let handle = try FileHandle(forReadingFrom: url)
    defer { try? handle.close() }
    var digest = SHA256()
    while let data = try handle.read(upToCount: 8 * 1024 * 1024), !data.isEmpty {
      try Task.checkCancellation()
      digest.update(data: data)
    }
    return digest.finalize().map { String(format: "%02x", $0) }.joined()
  }

  static func prefillRanges(tokenCount: Int, maxNewTokens: Int) throws -> [Range<Int>] {
    guard tokenCount > 0, maxNewTokens > 0,
          tokenCount <= contextLength, maxNewTokens <= contextLength - tokenCount else {
      throw InvalidInput.contextExceeded
    }
    return stride(from: 0, to: tokenCount, by: queryLength).map {
      $0..<min($0 + queryLength, tokenCount)
    }
  }

  /// A row may attend to the entire cached prefix and its own causal prefix.
  /// The finite FP16 minimum matches the upstream export/generation contract.
  static func causalMask(queryCount: Int, absoluteEnd: Int) throws -> [UInt16] {
    guard queryCount > 0, queryCount <= queryLength,
          absoluteEnd >= queryCount, absoluteEnd <= contextLength else {
      throw InvalidInput.maskDimensions
    }
    let pastCount = absoluteEnd - queryCount
    var mask = [UInt16](repeating: 0xFBFF, count: queryCount * absoluteEnd)
    for row in 0..<queryCount {
      for column in 0...(pastCount + row) {
        mask[row * absoluteEnd + column] = 0
      }
    }
    return mask
  }

  static func configuredStopTokenIDs(in folder: URL) throws -> Set<Int> {
    var result = Set<Int>()
    for filename in ["config.json", "generation_config.json"] {
      let url = folder.appendingPathComponent(filename)
      guard FileManager.default.fileExists(atPath: url.path) else { continue }
      let config = try JSONDecoder().decode(StopConfiguration.self, from: Data(contentsOf: url))
      result.formUnion(config.ids)
    }
    return result
  }

  static func validatedStopTokenIDs(in folder: URL) throws -> Set<Int> {
    let configured = try configuredStopTokenIDs(in: folder)
    guard configured == stopTokenIDs else { throw InvalidInput.stopTokenIDs }
    return configured
  }

  private struct StopConfiguration: Decodable {
    let ids: Set<Int>
    private enum CodingKeys: String, CodingKey { case eosTokenID = "eos_token_id" }

    init(from decoder: any Decoder) throws {
      let values = try decoder.container(keyedBy: CodingKeys.self)
      if try !values.contains(.eosTokenID) || values.decodeNil(forKey: .eosTokenID) {
        ids = []
      } else if let id = try? values.decode(Int.self, forKey: .eosTokenID) {
        guard id >= 0 else { throw InvalidInput.stopTokenIDs }
        ids = [id]
      } else {
        let decoded = try values.decode([Int].self, forKey: .eosTokenID)
        guard decoded.allSatisfy({ $0 >= 0 }) else { throw InvalidInput.stopTokenIDs }
        ids = Set(decoded)
      }
    }
  }
}
