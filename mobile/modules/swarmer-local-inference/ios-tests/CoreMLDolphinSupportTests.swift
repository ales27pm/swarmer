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
    try expect(CoreMLDolphinSupport.qualifiedMaxTokens(1024, isDolphin: true) == 32,
               "Dolphin native dispatch bypassed the qualification budget")
    try expect(CoreMLDolphinSupport.qualifiedMaxTokens(32, isDolphin: true) == 32,
               "The exact qualification budget changed")
    try expect(CoreMLDolphinSupport.qualifiedMaxTokens(8, isDolphin: true) == 8,
               "Qualification raised a smaller requested budget")
    try expect(CoreMLDolphinSupport.qualifiedMaxTokens(0, isDolphin: true) == 0,
               "Qualification hid an invalid request")
    try expect(CoreMLDolphinSupport.qualifiedMaxTokens(1024, isDolphin: false) == 1024,
               "Dolphin qualification changed the generic Core ML budget")
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
    try Data(#"{"eos_token_id":[128256,128001,128008,128009]}"#.utf8)
      .write(to: folder.appendingPathComponent("generation_config.json"))
    try expect(try CoreMLDolphinSupport.validatedStopTokenIDs(in: folder)
      == CoreMLDolphinSupport.stopTokenIDs, "Pinned EOS set changed")
    try Data(#"{"eos_token_id":42}"#.utf8)
      .write(to: folder.appendingPathComponent("generation_config.json"))
    try expect(try CoreMLDolphinSupport.configuredStopTokenIDs(in: folder)
      == CoreMLDolphinSupport.stopTokenIDs.union([42]), "Generic EOS configuration lost its union")
    try expectRejected { _ = try CoreMLDolphinSupport.validatedStopTokenIDs(in: folder) }
    try Data(#"{"eos_token_id":true}"#.utf8)
      .write(to: folder.appendingPathComponent("generation_config.json"))
    try expectRejected { _ = try CoreMLDolphinSupport.configuredStopTokenIDs(in: folder) }

    let identity = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(
      at: identity.appendingPathComponent("nested"), withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: identity) }
    let artifact = identity.appendingPathComponent("nested/model.bin")
    try Data("abc".utf8).write(to: artifact)
    let expected = [CoreMLDolphinSupport.ArtifactFile(
      path: "nested/model.bin", bytes: 3,
      sha256: "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )]
    try CoreMLDolphinSupport.verifyFileInventory(root: identity, expected: expected)
    try Data("abd".utf8).write(to: artifact)
    try expectRejected {
      try CoreMLDolphinSupport.verifyFileInventory(root: identity, expected: expected)
    }
    try expect(
      CoreMLDolphinSupport.officialRepositoryRevision
        == "4972a699bb5e9e72ecb054a9d0435e9f36f959e1",
      "Official immutable revision changed"
    )
    print("PASS: Dolphin Core ML identity, prefill, masks, context boundaries, and strict EOS")
  }
}
