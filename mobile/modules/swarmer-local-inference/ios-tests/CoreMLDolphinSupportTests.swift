import Foundation

@main
struct CoreMLDolphinSupportTests {
  private struct Failure: Error { let message: String }

  private static func expect(_ value: Bool, _ message: String) throws {
    if !value { throw Failure(message: message) }
  }

  private static func expectRejected(_ operation: () throws -> Void) throws {
    do {
      try operation()
    } catch is CoreMLDolphinSupport.InvalidInput {
      return
    } catch is DecodingError {
      return
    }
    throw Failure(message: "Invalid input was accepted")
  }

  static func main() throws {
    let ranges = try CoreMLDolphinSupport.prefillRanges(tokenCount: 1300, maxNewTokens: 748)
    try expect(ranges == [0..<512, 512..<1024, 1024..<1300], "Long prompts must retain every token")
    try expect(ranges.flatMap(Array.init) == Array(0..<1300), "Prefill chunks duplicated or dropped tokens")
    try expect(try CoreMLDolphinSupport.prefillRanges(tokenCount: 512, maxNewTokens: 1) == [0..<512],
               "Exact chunk boundary changed")
    try expectRejected { _ = try CoreMLDolphinSupport.prefillRanges(tokenCount: 1300, maxNewTokens: 749) }
    try expectRejected { _ = try CoreMLDolphinSupport.prefillRanges(tokenCount: 0, maxNewTokens: 1) }
    try expectRejected { _ = try CoreMLDolphinSupport.prefillRanges(tokenCount: 1, maxNewTokens: Int.max) }

    let first = try CoreMLDolphinSupport.causalMask(queryCount: 3, absoluteEnd: 3)
    try expect(first == [0, 0xFBFF, 0xFBFF, 0, 0, 0xFBFF, 0, 0, 0], "Initial prefill is not causal")
    let continuation = try CoreMLDolphinSupport.causalMask(queryCount: 2, absoluteEnd: 5)
    try expect(continuation == [0, 0, 0, 0, 0xFBFF, 0, 0, 0, 0, 0], "Cached prefix is masked incorrectly")
    let decode = try CoreMLDolphinSupport.causalMask(queryCount: 1, absoluteEnd: 2048)
    try expect(decode.count == 2048 && decode.allSatisfy({ $0 == 0 }), "Decode lost cached history")
    try expect(first[1] == 0xFBFF, "Masked attention differs from upstream FP16 minimum")
    try expectRejected { _ = try CoreMLDolphinSupport.causalMask(queryCount: 513, absoluteEnd: 513) }
    try expectRejected { _ = try CoreMLDolphinSupport.causalMask(queryCount: 3, absoluteEnd: 2) }
    try expectRejected { _ = try CoreMLDolphinSupport.causalMask(queryCount: 1, absoluteEnd: 2049) }

    let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: folder) }
    try Data(#"{"eos_token_id":[128256,128001,128008,128009]}"#.utf8)
      .write(to: folder.appendingPathComponent("config.json"))
    try Data(#"{"eos_token_id":42}"#.utf8)
      .write(to: folder.appendingPathComponent("generation_config.json"))
    try expect(try CoreMLDolphinSupport.configuredStopTokenIDs(in: folder)
      == CoreMLDolphinSupport.stopTokenIDs.union([42]), "EOS arrays and generation overrides were lost")
    try Data(#"{"eos_token_id":true}"#.utf8)
      .write(to: folder.appendingPathComponent("generation_config.json"))
    try expectRejected { _ = try CoreMLDolphinSupport.configuredStopTokenIDs(in: folder) }
    print("PASS: Dolphin Core ML prefill, causal masks, context boundaries, and EOS configuration")
  }
}
