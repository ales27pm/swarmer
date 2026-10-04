// A bounded allowlist for DEBUG resources, separate from the full-model registry.
// Kept independent of Core ML so its Release exclusion and manifest bounds can
// be tested without loading or compiling a model.
enum CoreMLProbeCatalog {
  static let maximumFixtureCount = 12
  #if DEBUG
  static let fixtureIDs: Set<String> = [
    "attention-stateful-fused", "attention-stateful-decomposed",
    "attention-stateless-fused", "attention-stateless-decomposed",
    "dolphin-attention-int4-block32", "dolphin-attention-int4-perchannel",
    "dolphin-attention-int4-perchannel-cache28",
    "dolphin-attention-int4-perchannel-cache28-two-blocks",
    "dolphin-attention-int4-perchannel-cache28-slot1",
    "dolphin-attention-int4-perchannel-cache28-two-blocks-independent",
    "dolphin-attention-int4-perchannel-cache28-two-blocks-separated-states",
    "dolphin-attention-int4-perchannel-cache2-two-blocks-separated-states",
  ]
  #else
  static let fixtureIDs: Set<String> = []
  #endif

  static func accepts(_ ids: [String]) -> Bool {
    (1...maximumFixtureCount).contains(ids.count)
      && Set(ids).count == ids.count
      && ids.allSatisfy { fixtureIDs.contains($0) }
  }
}
