@main
struct CoreMLProbeCatalogTests {
  private struct Failure: Error { let message: String }
  private static func expect(_ result: Bool, _ message: String) throws {
    if !result { throw Failure(message: message) }
  }

  static func main() throws {
    let previous = [
      "attention-stateful-fused", "attention-stateful-decomposed",
      "attention-stateless-fused", "attention-stateless-decomposed",
      "dolphin-attention-int4-block32", "dolphin-attention-int4-perchannel",
      "dolphin-attention-int4-perchannel-cache28",
      "dolphin-attention-int4-perchannel-cache28-two-blocks",
      "dolphin-attention-int4-perchannel-cache28-slot1",
      "dolphin-attention-int4-perchannel-cache28-two-blocks-independent",
      "dolphin-attention-int4-perchannel-cache28-two-blocks-separated-states",
    ]
    let separated = "dolphin-attention-int4-perchannel-cache2-two-blocks-separated-states"
    let all = previous + [separated]
    try expect(CoreMLProbeCatalog.maximumFixtureCount == 12, "Fixture budget changed unexpectedly")
    #if DEBUG
    try expect(CoreMLProbeCatalog.fixtureIDs == Set(all), "Catalog changed more than the twelfth fixture")
    try expect(CoreMLProbeCatalog.accepts(previous), "Previous eleven-fixture bundle must remain accepted")
    try expect(CoreMLProbeCatalog.accepts(all), "Twelve-fixture bundle must be accepted")
    for id in all {
      try expect(CoreMLProbeCatalog.accepts([id]), "Known fixture was rejected")
    }
    #else
    try expect(CoreMLProbeCatalog.fixtureIDs.isEmpty, "Release exposed diagnostic fixtures")
    for ids in [previous, all, [separated]] {
      try expect(!CoreMLProbeCatalog.accepts(ids), "Release accepted diagnostic resources")
    }
    #endif
    for invalid in [[], [separated, separated], all + [separated], all + ["unknown"],
                    ["unknown"], ["../" + separated], [separated + " "]] {
      try expect(!CoreMLProbeCatalog.accepts(invalid), "Empty, duplicate, oversized or unknown manifest accepted")
    }
    print("PASS: Core ML fixture catalog, count, identity, duplicates and Release exclusion")
  }
}
