import Foundation

/// ANEMLL Llama 3.2 1B FAST iOS 0.3.0 only (HF revision c6461a77).
/// This is a bounded context, not the tokenizer's 131,072-token training limit.
enum ANEMLLSupport {
  static let contextLength = 512
  static let batchSize = 64
  static let hiddenSize = 2048
  static let logitsCount = 8
  static let logitsWidth = 16032
  static let vocabularySize = logitsCount * logitsWidth
  static let stateName = "model_model_kv_cache_0"
  static let stateShape = [32, 8, contextLength, 64]
  static let paddingTokenID = 128004 // <|finetune_right_pad_id|> in the pinned tokenizer.
  static let stopTokenIDs: Set<Int> = [128001, 128008, 128009]

  enum InvalidInput: Error { case context, tokens, mask, logits, sampling, busy }

  struct Plan: Equatable, Sendable {
    let prefillRanges: [Range<Int>]
    let firstDecodePosition: Int
    let outputLimit: Int
  }

  static func plan(tokenCount: Int, requestedTokens: Int) throws -> Plan {
    guard tokenCount > 0, tokenCount < contextLength, requestedTokens > 0 else {
      throw InvalidInput.context
    }
    return Plan(
      prefillRanges: stride(from: 0, to: tokenCount, by: batchSize).map {
        $0..<min($0 + batchSize, tokenCount)
      },
      firstDecodePosition: tokenCount - 1,
      outputLimit: min(requestedTokens, contextLength - tokenCount)
    )
  }

  static func paddedTokens(_ tokens: [Int]) throws -> [Int32] {
    guard !tokens.isEmpty, tokens.count <= batchSize,
          tokens.allSatisfy({ (0..<vocabularySize).contains($0) }) else { throw InvalidInput.tokens }
    return tokens.map(Int32.init) + Array(repeating: Int32(paddingTokenID), count: batchSize - tokens.count)
  }

  /// FP16 -infinity matches the export. Even padded rows have a causal prefix;
  /// all-masked rows would produce NaNs in this model's manually lowered softmax.
  /// Decode uses its actual position p (NOT p+1), hiding every padded future slot.
  static func causalMask(start: Int, count: Int) throws -> [UInt16] {
    guard start >= 0, count == 1 || count == batchSize,
          start <= contextLength - count else { throw InvalidInput.mask }
    var values = [UInt16](repeating: 0xFC00, count: count * contextLength)
    for row in 0..<count {
      for column in 0...(start + row) { values[row * contextLength + column] = 0 }
    }
    return values
  }

  static func concatenateLogits(_ pieces: [[Float]]) throws -> [Float] {
    guard pieces.count == logitsCount,
          pieces.allSatisfy({ $0.count == logitsWidth && $0.allSatisfy(\.isFinite) }) else {
      throw InvalidInput.logits
    }
    return pieces.flatMap { $0 }
  }

  /// Temperature sampling over the complete ordered vocabulary, with stable
  /// softmax and deterministic first-index ties for greedy decoding.
  static func sample(_ logits: [Float], temperature: Double, draw: Double) throws -> Int {
    guard !logits.isEmpty, logits.allSatisfy(\.isFinite), temperature.isFinite,
          temperature >= 0, draw.isFinite, (0..<1).contains(draw) else { throw InvalidInput.sampling }
    var best = 0
    for index in logits.indices where logits[index] > logits[best] { best = index }
    if temperature <= 0.0001 { return best }
    let maximum = Double(logits[best])
    let weights = logits.map { exp((Double($0) - maximum) / temperature) }
    let total = weights.reduce(0, +)
    guard total.isFinite, total > 0 else { throw InvalidInput.sampling }
    let target = draw * total
    var cumulative = 0.0
    for (index, weight) in weights.enumerated() {
      cumulative += weight
      if weight > 0 && target < cumulative { return index }
    }
    return weights.lastIndex(where: { $0 > 0 }) ?? best
  }

  /// Cancel/unload invalidate an operation but do not release its slot while an
  /// awaited Core ML call still owns models/state. A new operation must wait for
  /// finish; stale completion cannot publish loaded models or generated tokens.
  struct OperationGate {
    private(set) var active: UUID?
    private var cancelled = false

    mutating func begin() throws -> UUID {
      guard active == nil else { throw InvalidInput.busy }
      let id = UUID()
      active = id
      cancelled = false
      return id
    }

    func accepts(_ id: UUID) -> Bool { active == id && !cancelled }
    mutating func cancel() { cancelled = true }
    mutating func finish(_ id: UUID) {
      guard active == id else { return }
      active = nil
      cancelled = false
    }
  }
}
