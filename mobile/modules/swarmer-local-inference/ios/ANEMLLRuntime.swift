import CoreML
import Foundation
import Tokenizers

/// Three compiled components, four function loads, one fresh shared cache per
/// generation. The contract is the pinned ANEMLL 0.3.0 Llama 1B export only.
@available(iOS 18.0, *)
actor ANEMLLRuntime {
  // Core ML imports MLModel/MLState as non-Sendable. These wrappers retain stable
  // identity; OperationGate prevents overlapping load/generation calls. State
  // belongs exclusively to one Context and is never read or reused after it ends.
  private final class Component: @unchecked Sendable {
    let model: MLModel
    init(_ model: MLModel) { self.model = model }
  }

  private struct Models {
    let embeddings: Component
    let head: Component
    let infer: Component
    let prefill: Component

    func makeContext() -> Context {
      Context(embeddings: embeddings.model, head: head.model,
              infer: infer.model, prefill: prefill.model)
    }
  }

  private final class Context: @unchecked Sendable {
    let embeddings: MLModel
    let head: MLModel
    let infer: MLModel
    let prefill: MLModel
    let state: MLState

    init(embeddings: MLModel, head: MLModel, infer: MLModel, prefill: MLModel) {
      self.embeddings = embeddings
      self.head = head
      self.infer = infer
      self.prefill = prefill
      state = prefill.makeState()
    }

    func embed(_ ids: [Int32]) async throws -> [String: MLTensor] {
      try await embeddings.prediction(from: ["input_ids": MLTensor(shape: [1, ids.count], scalars: ids)])
    }

    func forward(_ inputs: [String: MLTensor], prefill isPrefill: Bool) async throws -> [String: MLTensor] {
      try await (isPrefill ? prefill : infer).prediction(from: inputs, using: state)
    }

    func logits(_ hidden: MLTensor) async throws -> [String: MLTensor] {
      try await head.prediction(from: ["hidden_states": hidden])
    }
  }

  private var models: Models?
  private var tokenizer: (any Tokenizer)?
  private var gate = ANEMLLSupport.OperationGate()

  func load(modelURL: URL, tokenizerURL: URL,
            diagnosticUnits: CoreMLDiagnosticComputeUnits? = nil) async throws -> CoreMLLoadDiagnostic? {
    let units = try CoreMLDiagnosticComputeUnits.requested(diagnosticUnits?.rawValue, runtime: "coreml")
    let operation = try begin()
    defer { gate.finish(operation) }
    models = nil
    tokenizer = nil
    let startedAt = ProcessInfo.processInfo.systemUptime
    var stage = CoreMLLoadDiagnostic.Stage.validateArtifact
    do {
      guard modelURL.standardizedFileURL == tokenizerURL.standardizedFileURL else {
        throw LocalInferenceError.unsupportedModel("ANEMLL tokenizer must share the component root")
      }
      try ANEMLLModelProfile.validateDirectory(modelURL)
      try check(operation)
      // Use the same synchronous initializer as the official 0.3.0 loader and
      // the isolated device load proof, off the actor executor. No compilation,
      // alternate compute-unit retry or fallback is performed.
      stage = .load
      let embeddings = try await Self.loadComponent(modelURL, "llama_embeddings_lut8.mlmodelc", nil, units)
      try check(operation)
      let head = try await Self.loadComponent(modelURL, "llama_lm_head_lut8.mlmodelc", nil, units)
      try check(operation)
      let infer = try await Self.loadComponent(modelURL, "llama_FFN_PF_lut4_chunk_01of01.mlmodelc", "infer", units)
      try check(operation)
      let prefill = try await Self.loadComponent(modelURL, "llama_FFN_PF_lut4_chunk_01of01.mlmodelc", "prefill", units)
      try check(operation)
      stage = .validateContract
      try Self.validate(embeddings: embeddings.model, head: head.model, infer: infer.model, prefill: prefill.model)
      stage = .tokenizer
      let loadedTokenizer = try await AutoTokenizer.from(modelFolder: tokenizerURL, strict: true)
      try check(operation)
      let specialTokens = [
        "<|begin_of_text|>": 128000, "<|end_of_text|>": 128001,
        "<|finetune_right_pad_id|>": ANEMLLSupport.paddingTokenID,
        "<|eom_id|>": 128008, "<|eot_id|>": 128009,
      ]
      guard loadedTokenizer.hasChatTemplate, loadedTokenizer.bosTokenId == 128000,
            loadedTokenizer.eosTokenId == 128009,
            specialTokens.allSatisfy({ loadedTokenizer.convertTokenToId($0.key) == $0.value }) else {
        throw LocalInferenceError.unsupportedCoreMLContract("ANEMLL requires the Llama 3.2 chat tokenizer")
      }
      models = Models(embeddings: embeddings, head: head, infer: infer, prefill: prefill)
      tokenizer = loadedTokenizer
      return units.map { .capture(units: $0, stage: .complete, startedAt: startedAt) }
    } catch {
      let failure: any Error = gate.accepts(operation) && !Task.isCancelled ? error : CancellationError()
      guard let units else { throw failure }
      throw CoreMLDiagnosticLoadFailure(diagnostic: .capture(
        units: units, stage: stage, startedAt: startedAt, error: failure
      ))
    }
  }

  func generate(prompt: String, maxTokens: Int, temperature: Double) async throws -> RuntimeGenerationResult {
    guard let models, let tokenizer else { throw LocalInferenceError.modelNotLoaded }
    let operation = try begin()
    defer { gate.finish(operation) }
    guard !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw LocalInferenceError.promptEmpty }
    guard temperature.isFinite, temperature >= 0 else { throw LocalInferenceError.invalidTemperature }
    guard maxTokens > 0 else { throw LocalInferenceError.invalidMaxTokens }
    let tokens = try tokenizer.applyChatTemplate(messages: [["role": "user", "content": prompt]])
    guard !tokens.isEmpty, tokens.allSatisfy({ (0..<ANEMLLSupport.vocabularySize).contains($0) }) else {
      throw LocalInferenceError.inferenceFailed("ANEMLL tokenizer returned invalid IDs")
    }
    let plan: ANEMLLSupport.Plan
    do { plan = try ANEMLLSupport.plan(tokenCount: tokens.count, requestedTokens: maxTokens) }
    catch { throw LocalInferenceError.contextExceeded }
    var generated: [Int] = []
    func result(_ reason: String) -> RuntimeGenerationResult {
      RuntimeGenerationResult(text: tokenizer.decode(tokens: generated, skipSpecialTokens: true),
                              finishReason: reason, tokenCount: generated.count)
    }
    do {
      try check(operation)
      let context = models.makeContext()
      for range in plan.prefillRanges {
        let ids = try ANEMLLSupport.paddedTokens(Array(tokens[range]))
        let embedded = try await context.embed(ids)
        try check(operation)
        let hidden = try Self.tensor(embedded, "hidden_states", [1, ANEMLLSupport.batchSize, ANEMLLSupport.hiddenSize])
        let inputs = try Self.inputs(hidden, start: range.lowerBound, count: ANEMLLSupport.batchSize)
        let output = try await context.forward(inputs, prefill: true)
        try check(operation)
        _ = try Self.tensor(output, "output_hidden_states", [1, 1, ANEMLLSupport.hiddenSize])
        // Its first-position, unnormalized output is not usable as LM-head input.
      }
      for step in 0..<plan.outputLimit {
        try check(operation)
        let token = generated.last ?? tokens[tokens.count - 1]
        let embedded = try await context.embed([Int32(token)])
        try check(operation)
        let hidden = try Self.tensor(embedded, "hidden_states", [1, 1, ANEMLLSupport.hiddenSize])
        let inputs = try Self.inputs(hidden, start: plan.firstDecodePosition + step, count: 1)
        let output = try await context.forward(inputs, prefill: false)
        try check(operation)
        let normalized = try Self.tensor(output, "output_hidden_states", [1, 1, ANEMLLSupport.hiddenSize])
        let logits = try await context.logits(normalized)
        try check(operation)
        guard Set(logits.keys) == Set((1...8).map { "logits\($0)" }) else {
          throw LocalInferenceError.unsupportedCoreMLContract("ANEMLL omitted a logits partition")
        }
        var pieces: [[Float]] = []
        for index in 1...ANEMLLSupport.logitsCount {
          let part = try Self.tensor(logits, "logits\(index)", [1, 1, ANEMLLSupport.logitsWidth])
          // Materialize the FP16 result, then convert scalars on CPU. Do not
          // introduce a separate MLTensor cast graph with its own device choice.
          pieces.append(await part.shapedArray(of: Float16.self).scalars.map(Float.init))
          try check(operation)
        }
        let next = try ANEMLLSupport.sample(ANEMLLSupport.concatenateLogits(pieces),
                                           temperature: temperature, draw: Double.random(in: 0..<1))
        if ANEMLLSupport.stopTokenIDs.contains(next) { return result("stop") }
        generated.append(next)
      }
      return result("length")
    } catch {
      if error is CancellationError || !gate.accepts(operation) || Task.isCancelled { return result("cancelled") }
      throw error
    }
  }

  func cancel() { gate.cancel() }
  func unload() { gate.cancel(); models = nil; tokenizer = nil }

  private func begin() throws -> UUID {
    do { return try gate.begin() }
    catch { throw LocalInferenceError.generationInProgress }
  }

  private func check(_ operation: UUID) throws {
    guard gate.accepts(operation), !Task.isCancelled else { throw CancellationError() }
  }

  private nonisolated static func loadComponent(_ root: URL, _ name: String, _ function: String?,
                                                _ units: CoreMLDiagnosticComputeUnits?) async throws -> Component {
    try await Task.detached {
      let configuration = MLModelConfiguration()
      configuration.functionName = function
      switch units ?? .cpuAndNeuralEngine {
      case .all: configuration.computeUnits = .all
      case .cpuOnly: configuration.computeUnits = .cpuOnly
      case .cpuAndGPU: configuration.computeUnits = .cpuAndGPU
      case .cpuAndNeuralEngine: configuration.computeUnits = .cpuAndNeuralEngine
      }
      return Component(try MLModel(contentsOf: root.appendingPathComponent(name), configuration: configuration))
    }.value
  }

  private static func tensor(_ outputs: [String: MLTensor], _ name: String, _ shape: [Int]) throws -> MLTensor {
    guard let tensor = outputs[name], tensor.shape == shape, tensor.scalarType == Float16.self else {
      throw LocalInferenceError.unsupportedCoreMLContract("ANEMLL returned an unexpected tensor contract")
    }
    return tensor
  }

  private static func inputs(_ hidden: MLTensor, start: Int, count: Int) throws -> [String: MLTensor] {
    let bits = try ANEMLLSupport.causalMask(start: start, count: count)
    return [
      "hidden_states": hidden,
      "position_ids": MLTensor(shape: [count], scalars: (start..<(start + count)).map(Int32.init)),
      "current_pos": MLTensor(shape: [1], scalars: [Int32(start)]),
      "causal_mask": MLTensor(shape: [1, 1, count, ANEMLLSupport.contextLength], scalars: bits.map(Float16.init(bitPattern:))),
    ]
  }

  private static func validate(embeddings: MLModel, head: MLModel, infer: MLModel, prefill: MLModel) throws {
    func require(_ condition: Bool) throws {
      guard condition else { throw LocalInferenceError.unsupportedCoreMLContract("unsupported ANEMLL 0.3.0 graph") }
    }
    func feature(_ desc: MLFeatureDescription?, _ shape: [Int], _ type: MLMultiArrayDataType) throws {
      try require(desc?.isOptional == false && desc?.type == .multiArray)
      guard let constraint = desc?.multiArrayConstraint else {
        throw LocalInferenceError.unsupportedCoreMLContract("missing ANEMLL tensor")
      }
      try require(constraint.dataType == type && constraint.shape.map(\.intValue) == shape)
      try require(constraint.shapeConstraint.type == .unspecified)
    }
    let embedding = embeddings.modelDescription
    try require(Set(embedding.inputDescriptionsByName.keys) == ["input_ids"])
    try require(Set(embedding.outputDescriptionsByName.keys) == ["hidden_states"] && embedding.stateDescriptionsByName.isEmpty)
    guard let ids = embedding.inputDescriptionsByName["input_ids"], !ids.isOptional,
          let constraint = ids.multiArrayConstraint else {
      throw LocalInferenceError.unsupportedCoreMLContract("missing ANEMLL token input")
    }
    try require(constraint.dataType == .int32 && constraint.shape.map(\.intValue) == [1, 1])
    try require(constraint.shapeConstraint.type == .enumerated)
    try require(Set(constraint.shapeConstraint.enumeratedShapes.map { $0.map(\.intValue) }) == Set([[1, 1], [1, 64]]))
    // The embedding export intentionally leaves its output shape unspecified.
    // Every prediction checks the resolved batch and hidden dimensions above.
    try feature(embedding.outputDescriptionsByName["hidden_states"], [], .float16)
    let lm = head.modelDescription
    try require(Set(lm.inputDescriptionsByName.keys) == ["hidden_states"] && lm.stateDescriptionsByName.isEmpty)
    try feature(lm.inputDescriptionsByName["hidden_states"], [1, 1, 2048], .float16)
    try require(Set(lm.outputDescriptionsByName.keys) == Set((1...8).map { "logits\($0)" }))
    for index in 1...8 { try feature(lm.outputDescriptionsByName["logits\(index)"], [1, 1, 16032], .float16) }
    for (model, count) in [(infer, 1), (prefill, 64)] {
      let desc = model.modelDescription
      try require(Set(desc.inputDescriptionsByName.keys) == ["hidden_states", "position_ids", "causal_mask", "current_pos"])
      try feature(desc.inputDescriptionsByName["hidden_states"], [1, count, 2048], .float16)
      try feature(desc.inputDescriptionsByName["position_ids"], [count], .int32)
      try feature(desc.inputDescriptionsByName["causal_mask"], [1, 1, count, 512], .float16)
      try feature(desc.inputDescriptionsByName["current_pos"], [1], .int32)
      try require(Set(desc.outputDescriptionsByName.keys) == ["output_hidden_states"])
      try feature(desc.outputDescriptionsByName["output_hidden_states"], [1, 1, 2048], .float16)
      try require(Set(desc.stateDescriptionsByName.keys) == [ANEMLLSupport.stateName])
      guard let state = desc.stateDescriptionsByName[ANEMLLSupport.stateName]?.stateConstraint else {
        throw LocalInferenceError.unsupportedCoreMLContract("missing ANEMLL state")
      }
      try require(state.dataType == .float16 && state.bufferShape == ANEMLLSupport.stateShape)
    }
  }
}
