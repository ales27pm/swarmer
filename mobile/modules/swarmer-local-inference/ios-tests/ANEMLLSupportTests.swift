import Foundation

@main
struct ANEMLLSupportTests {
  private struct Failure: Error { let message: String }
  private static func expect(_ value: Bool, _ message: String) throws {
    if !value { throw Failure(message: message) }
  }
  private static func rejects(_ operation: () throws -> Void) throws {
    do { try operation() } catch is ANEMLLSupport.InvalidInput { return }
    throw Failure(message: "invalid input accepted")
  }

  static func main() throws {
    // Actual Core ML fixed tensors expose one enumerated shape, not unspecified.
    // The embedding output alone has no shape until prediction resolves it.
    for shape in [[1, 1, 2048], [1, 1, 16032], [1, 64, 2048], [64], [1], [1, 1, 64, 512], [1, 1, 1, 512]] {
      try expect(ANEMLLSupport.matchesFeatureShape(shape, expected: shape, constraint: .enumerated([shape])),
                 "fixed Core ML singleton rejected")
      try expect(!ANEMLLSupport.matchesFeatureShape(shape, expected: shape, constraint: .enumerated([shape, [2]])),
                 "alternative shape accepted")
      try expect(!ANEMLLSupport.matchesFeatureShape(shape, expected: shape, constraint: .enumerated([shape, shape])),
                 "non-singleton shape list accepted")
      try expect(!ANEMLLSupport.matchesFeatureShape(shape, expected: shape, constraint: .enumerated([[2]])),
                 "different enumerated shape accepted")
      try expect(!ANEMLLSupport.matchesFeatureShape([2], expected: shape, constraint: .enumerated([shape])),
                 "different default shape accepted")
      try expect(!ANEMLLSupport.matchesFeatureShape(shape, expected: shape, constraint: .enumerated([])),
                 "missing fixed shape accepted")
      try expect(!ANEMLLSupport.matchesFeatureShape(shape, expected: shape, constraint: .unspecified),
                 "unspecified fixed shape accepted")
      try expect(!ANEMLLSupport.matchesFeatureShape(shape, expected: shape, constraint: .unsupported),
                 "range or unknown shape constraint accepted")
    }
    try expect(ANEMLLSupport.matchesFeatureShape([], expected: [], constraint: .unspecified), "unresolved embedding shape rejected")
    try expect(!ANEMLLSupport.matchesFeatureShape([], expected: [], constraint: .enumerated([[]])), "empty enumerated shape accepted")
    try expect(!ANEMLLSupport.matchesFeatureShape([], expected: [], constraint: .unsupported), "empty range accepted")
    print("PASS: exact fixed singleton and empty unspecified Core ML shapes; alternative/range shapes rejected")

    // No truncation, omission, duplicated prompt token, or out-of-cache batch.
    for count in [1, 63, 64, 65, 511] {
      let plan = try ANEMLLSupport.plan(tokenCount: count, requestedTokens: 1024)
      try expect(plan.prefillRanges.flatMap(Array.init) == Array(0..<count), "prompt coverage")
      try expect(plan.firstDecodePosition == count - 1 && plan.outputLimit == 512 - count, "decode/ceiling")
      for range in plan.prefillRanges {
        let input = Array(range)
        let padded = try ANEMLLSupport.paddedTokens(input)
        try expect(padded.count == 64 && Array(padded.prefix(input.count)) == input.map(Int32.init), "padding changed prompt")
        try expect(padded.dropFirst(input.count).allSatisfy { $0 == 128004 }, "padding not explicit")
        let mask = try ANEMLLSupport.causalMask(start: range.lowerBound, count: 64)
        try expect(mask.count == 64 * 512, "prefill mask shape")
        for row in 0..<64 {
          let zeroEnd = range.lowerBound + row
          try expect(mask[(row * 512)...(row * 512 + zeroEnd)].allSatisfy { $0 == 0 }, "prefix hidden")
          try expect(mask[(row * 512 + zeroEnd + 1)..<((row + 1) * 512)].allSatisfy { $0 == 0xFC00 }, "future visible")
        }
      }
      let mask = try ANEMLLSupport.causalMask(start: plan.firstDecodePosition, count: 1)
      try expect(mask.prefix(count).allSatisfy { $0 == 0 } && mask.dropFirst(count).allSatisfy { $0 == 0xFC00 },
                 "decode exposed padding/off-by-one")
    }
    try expect(try ANEMLLSupport.plan(tokenCount: 32, requestedTokens: 3).outputLimit == 3, "caller output ceiling changed")
    try rejects { _ = try ANEMLLSupport.plan(tokenCount: 512, requestedTokens: 1) }
    try rejects { _ = try ANEMLLSupport.plan(tokenCount: 0, requestedTokens: 1) }
    try rejects { _ = try ANEMLLSupport.plan(tokenCount: 1, requestedTokens: 0) }
    try rejects { _ = try ANEMLLSupport.plan(tokenCount: Int.max, requestedTokens: 1) }
    try rejects { _ = try ANEMLLSupport.causalMask(start: 449, count: 64) }
    try rejects { _ = try ANEMLLSupport.causalMask(start: -1, count: 1) }
    try rejects { _ = try ANEMLLSupport.causalMask(start: 512, count: 1) }
    try rejects { _ = try ANEMLLSupport.causalMask(start: 0, count: 2) }
    try rejects { _ = try ANEMLLSupport.paddedTokens([128256]) }
    try rejects { _ = try ANEMLLSupport.paddedTokens([-1]) }
    try rejects { _ = try ANEMLLSupport.paddedTokens(Array(repeating: 0, count: 65)) }

    var pieces = Array(repeating: Array(repeating: Float(-1000), count: 16032), count: 8)
    pieces[7][16031] = 9
    var joined = try ANEMLLSupport.concatenateLogits(pieces)
    try expect(joined.count == 128256, "vocabulary shape")
    try expect(try ANEMLLSupport.sample(joined, temperature: 0, draw: 0) == 128255, "eighth partition index")
    pieces[6][15] = 10
    pieces[7][15] = 10
    joined = try ANEMLLSupport.concatenateLogits(pieces)
    try expect(try ANEMLLSupport.sample(joined, temperature: 0, draw: 0) == 6 * 16032 + 15, "tie must keep lowest global ID")
    try expect(try ANEMLLSupport.sample([-10000, 10000], temperature: 0.1, draw: 0) == 1, "draw0 chose zero probability")
    try expect(try ANEMLLSupport.sample([0, 0], temperature: 1, draw: 0.25) == 0, "sampling lower interval")
    try expect(try ANEMLLSupport.sample([0, 0], temperature: 1, draw: 0.75) == 1, "sampling upper interval")
    try rejects { _ = try ANEMLLSupport.concatenateLogits(Array(pieces.dropLast())) }
    pieces[0][0] = .nan
    try rejects { _ = try ANEMLLSupport.concatenateLogits(pieces) }
    for temperature in [-1, Double.nan, Double.infinity] {
      try rejects { _ = try ANEMLLSupport.sample([1], temperature: temperature, draw: 0) }
    }
    try rejects { _ = try ANEMLLSupport.sample([.infinity], temperature: 1, draw: 0) }
    try rejects { _ = try ANEMLLSupport.sample([1], temperature: 1, draw: 1) }
    try expect(ANEMLLSupport.stopTokenIDs == [128001, 128008, 128009], "Llama stop tokens changed")
    try expect(!ANEMLLSupport.stopTokenIDs.contains(128004), "padding treated as EOS")

    // Model/state ownership survives cancel and unload until in-flight await has
    // returned. A stale completion cannot affect the next independent request.
    var gate = ANEMLLSupport.OperationGate()
    let first = try gate.begin()
    try expect(gate.accepts(first), "first operation rejected")
    try rejects { _ = try gate.begin() }
    gate.cancel()
    try expect(!gate.accepts(first), "cancel failed")
    try rejects { _ = try gate.begin() }
    gate.finish(UUID())
    try rejects { _ = try gate.begin() }
    gate.finish(first)
    let next = try gate.begin()
    gate.finish(first)
    try expect(gate.accepts(next) && !gate.accepts(first), "stale finish changed new request")
    gate.finish(next)
    print("PASS: ANEMLL batch64/context512, causal/padding boundaries, eight logits, sampling, EOS, cancellation isolation")
  }
}
