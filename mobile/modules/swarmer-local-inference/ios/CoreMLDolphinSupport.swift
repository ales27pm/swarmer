import Foundation

/// The published Dolphin stateful INT4 package has a 512-token query window and
/// a separate 2,048-token KV cache. These limits must not be conflated.
enum CoreMLDolphinSupport {
  static let contextLength = 2048
  static let queryLength = 512
  static let vocabularySize = 128258
  static let cacheShape = [28, 1, 8, contextLength, 128]
  static let stopTokenIDs: Set<Int> = [128256, 128001, 128008, 128009]

  enum InvalidInput: Error {
    case contextExceeded
    case maskDimensions
    case stopTokenIDs
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
