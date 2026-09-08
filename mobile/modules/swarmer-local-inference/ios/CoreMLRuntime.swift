import CoreML
import Foundation
import Tokenizers

@available(iOS 18.0, *)
actor CoreMLRuntime {
  // MLModel and MLState are imported as non-Sendable by the iOS 26.5 SDK even though
  // prediction is asynchronous. A generation context never escapes this runtime and
  // generating prevents overlapping predictions; cancellation/unload only update actor
  // flags while the in-flight context retains exclusive access and stable object identity.
  // TODO: Remove these wrappers when Core ML exposes Sendable or nonsending prediction APIs.
  private final class PredictionContext: @unchecked Sendable {
    private let model: MLModel
    private let state: MLState?

    init(model: MLModel, stateful: Bool) {
      self.model = model
      state = stateful ? model.makeState() : nil
    }

    func predict(from inputs: [String: MLTensor]) async throws -> [String: MLTensor] {
      if let state {
        return try await model.prediction(from: inputs, using: state)
      }
      return try await model.prediction(from: inputs)
    }
  }

  private final class LoadedModel {
    private let value: MLModel

    init(_ value: MLModel) {
      self.value = value
    }

    func makePredictionContext(stateful: Bool) -> PredictionContext {
      PredictionContext(model: value, stateful: stateful)
    }
  }

  private struct Contract: Sendable {
    enum SequenceShape: Sendable {
      case enumerated([Int])
      case range(minimum: Int, maximum: Int)
      case fixed(Int)

      var maximum: Int {
        switch self {
        case .enumerated(let values): values.max() ?? 0
        case .range(_, let maximum): maximum
        case .fixed(let value): value
        }
      }

      func accepts(_ count: Int) -> Bool {
        switch self {
        case .enumerated(let values): values.contains(count)
        case .range(let minimum, let maximum): (minimum...maximum).contains(count)
        case .fixed(let value): value == count
        }
      }

      func paddedLength(for count: Int) throws -> Int {
        switch self {
        case .enumerated(let values):
          guard let match = values.sorted().first(where: { $0 >= count }) else {
            throw LocalInferenceError.contextExceeded
          }
          return match
        case .range(let minimum, let maximum):
          guard count <= maximum else { throw LocalInferenceError.contextExceeded }
          return max(minimum, count)
        case .fixed(let value):
          guard count <= value else { throw LocalInferenceError.contextExceeded }
          return value
        }
      }
    }

    let inputIdsName: String
    let attentionMaskName: String?
    let logitsName: String
    let sequenceShape: SequenceShape
    let stateful: Bool
  }

  private var model: LoadedModel?
  private var tokenizer: (any Tokenizer)?
  private var contract: Contract?
  private var cancelRequested = false
  private var generating = false

  func load(modelURL: URL, tokenizerURL: URL) async throws {
    guard !generating else { throw LocalInferenceError.generationInProgress }
    cancelRequested = false
    let ext = modelURL.pathExtension.lowercased()
    guard ["mlmodel", "mlpackage", "mlmodelc"].contains(ext) else {
      throw LocalInferenceError.unsupportedModel("unsupported Core ML extension")
    }

    let compiledURL: URL
    if ext == "mlmodelc" {
      compiledURL = modelURL
    } else {
      compiledURL = try await MLModel.compileModel(at: modelURL)
    }
    try checkCancellation()

    let configuration = MLModelConfiguration()
    configuration.computeUnits = .all
    let loadedModel = try await MLModel.load(contentsOf: compiledURL, configuration: configuration)
    try checkCancellation()
    let loadedContract = try Self.validate(model: loadedModel)
    let loadedTokenizer = try await AutoTokenizer.from(modelFolder: tokenizerURL, strict: true)
    try checkCancellation()

    model = LoadedModel(loadedModel)
    contract = loadedContract
    tokenizer = loadedTokenizer
    cancelRequested = false
  }

  func generate(prompt: String, maxTokens: Int, temperature: Double) async throws -> RuntimeGenerationResult {
    guard !generating else { throw LocalInferenceError.generationInProgress }
    guard let model, let tokenizer, let contract else { throw LocalInferenceError.modelNotLoaded }
    generating = true
    cancelRequested = false
    defer {
      generating = false
      cancelRequested = false
    }

    var allTokens = tokenizer.encode(text: prompt, addSpecialTokens: true)
    guard !allTokens.isEmpty, allTokens.allSatisfy({ Int32(exactly: $0) != nil }) else {
      throw LocalInferenceError.inferenceFailed("the tokenizer returned invalid token IDs")
    }
    guard allTokens.count + maxTokens <= contract.sequenceShape.maximum else {
      throw LocalInferenceError.contextExceeded
    }
    if contract.stateful {
      guard contract.sequenceShape.accepts(allTokens.count), contract.sequenceShape.accepts(1) else {
        throw LocalInferenceError.unsupportedCoreMLContract(
          "stateful models must accept both the complete prompt length and one-token extension inputs"
        )
      }
    }

    let predictionContext = model.makePredictionContext(stateful: contract.stateful)
    var generatedTokens: [Int] = []
    generatedTokens.reserveCapacity(maxTokens)

    for step in 0..<maxTokens {
      if cancelRequested || Task.isCancelled {
        return RuntimeGenerationResult(
          text: tokenizer.decode(tokens: generatedTokens, skipSpecialTokens: true),
          finishReason: "cancelled",
          tokenCount: generatedTokens.count
        )
      }

      let predictionTokens: [Int]
      if contract.stateful, step > 0, let last = generatedTokens.last {
        predictionTokens = [last]
      } else {
        predictionTokens = allTokens
      }
      let targetLength = contract.stateful
        ? predictionTokens.count
        : try contract.sequenceShape.paddedLength(for: predictionTokens.count)
      let paddingCount = targetLength - predictionTokens.count
      let paddedTokens = predictionTokens.map(Int32.init) + [Int32](repeating: 0, count: paddingCount)
      var inputs = [
        contract.inputIdsName: MLTensor(shape: [1, targetLength], scalars: paddedTokens)
      ]
      if let attentionMaskName = contract.attentionMaskName {
        let mask = [Int32](repeating: 1, count: predictionTokens.count)
          + [Int32](repeating: 0, count: paddingCount)
        inputs[attentionMaskName] = MLTensor(shape: [1, targetLength], scalars: mask)
      }

      let outputs = try await predictionContext.predict(from: inputs)
      if cancelRequested || Task.isCancelled {
        return RuntimeGenerationResult(
          text: tokenizer.decode(tokens: generatedTokens, skipSpecialTokens: true),
          finishReason: "cancelled",
          tokenCount: generatedTokens.count
        )
      }

      guard let logits = outputs[contract.logitsName] else {
        throw LocalInferenceError.unsupportedCoreMLContract("the prediction omitted logits")
      }
      let nextToken = try await Self.sample(
        logits: logits,
        tokenIndex: predictionTokens.count - 1,
        temperature: temperature
      )
      if nextToken == tokenizer.eosTokenId {
        return RuntimeGenerationResult(
          text: tokenizer.decode(tokens: generatedTokens, skipSpecialTokens: true),
          finishReason: "stop",
          tokenCount: generatedTokens.count
        )
      }
      generatedTokens.append(nextToken)
      if !contract.stateful { allTokens.append(nextToken) }
    }

    return RuntimeGenerationResult(
      text: tokenizer.decode(tokens: generatedTokens, skipSpecialTokens: true),
      finishReason: "length",
      tokenCount: generatedTokens.count
    )
  }

  func cancel() {
    cancelRequested = true
  }

  func unload() {
    cancelRequested = true
    model = nil
    tokenizer = nil
    contract = nil
  }

  private func checkCancellation() throws {
    if cancelRequested || Task.isCancelled { throw CancellationError() }
  }

  private static func validate(model: MLModel) throws -> Contract {
    let description = model.modelDescription
    let inputs = description.inputDescriptionsByName
    let inputIdNames = ["inputIds", "input_ids"].filter { inputs[$0] != nil }
    guard inputIdNames.count == 1, let inputIds = inputs[inputIdNames[0]],
          let inputConstraint = inputIds.multiArrayConstraint else {
      throw LocalInferenceError.unsupportedCoreMLContract(
        "exactly one Int32 input named inputIds or input_ids is required"
      )
    }
    guard inputConstraint.dataType == .int32 else {
      throw LocalInferenceError.unsupportedCoreMLContract("input token IDs must use Int32")
    }

    let attentionNames = ["attentionMask", "attention_mask"].filter { inputs[$0] != nil }
    guard attentionNames.count <= 1 else {
      throw LocalInferenceError.unsupportedCoreMLContract("multiple attention-mask inputs were found")
    }
    if let attentionName = attentionNames.first,
       inputs[attentionName]?.multiArrayConstraint?.dataType != .int32 {
      throw LocalInferenceError.unsupportedCoreMLContract("the attention mask must use Int32")
    }

    let allowedInputs = Set(inputIdNames + attentionNames)
    let unexpectedInputs = Set(inputs.keys).subtracting(allowedInputs)
    guard unexpectedInputs.isEmpty else {
      throw LocalInferenceError.unsupportedCoreMLContract(
        "unsupported inputs: \(unexpectedInputs.sorted().joined(separator: ", "))"
      )
    }

    let outputs = description.outputDescriptionsByName
    guard Set(outputs.keys) == Set(["logits"]), let logits = outputs["logits"],
          let logitsConstraint = logits.multiArrayConstraint else {
      throw LocalInferenceError.unsupportedCoreMLContract("a multi-array output named logits is required")
    }
    switch logitsConstraint.dataType {
    case .float16, .float32, .double:
      break
    default:
      throw LocalInferenceError.unsupportedCoreMLContract("logits must use a floating-point type")
    }
    let logitsShape = logitsConstraint.shape.map(\.intValue)
    guard logitsShape.count == 3, logitsShape[0] == 1 else {
      throw LocalInferenceError.unsupportedCoreMLContract(
        "logits must have shape [1, sequence, vocabulary]"
      )
    }

    let defaultShape = inputConstraint.shape.map(\.intValue)
    guard defaultShape.count == 2, defaultShape[0] == 1 else {
      throw LocalInferenceError.unsupportedCoreMLContract("input token IDs must have shape [1, sequence]")
    }
    let shapeConstraint = inputConstraint.shapeConstraint
    let sequenceShape: Contract.SequenceShape
    switch shapeConstraint.type {
    case .enumerated:
      let lengths = shapeConstraint.enumeratedShapes.compactMap { shape -> Int? in
        guard shape.count == 2, shape[0].intValue == 1 else { return nil }
        return shape[1].intValue
      }
      guard lengths.count == shapeConstraint.enumeratedShapes.count, !lengths.isEmpty else {
        throw LocalInferenceError.unsupportedCoreMLContract("invalid enumerated token shapes")
      }
      sequenceShape = .enumerated(Array(Set(lengths)).sorted())
    case .range:
      let ranges = shapeConstraint.sizeRangeForDimension
      guard ranges.count == 2, ranges[0].rangeValue.contains(1) else {
        throw LocalInferenceError.unsupportedCoreMLContract("the batch dimension must accept one")
      }
      let sequenceRange = ranges[1].rangeValue
      let minimum = sequenceRange.location
      let (upperExclusive, overflow) = minimum.addingReportingOverflow(sequenceRange.length)
      guard !overflow, minimum > 0, sequenceRange.length > 0, upperExclusive > minimum else {
        throw LocalInferenceError.unsupportedCoreMLContract("invalid sequence-length range")
      }
      let maximum = upperExclusive - 1
      sequenceShape = .range(minimum: minimum, maximum: maximum)
    case .unspecified:
      guard defaultShape[1] > 0 else {
        throw LocalInferenceError.unsupportedCoreMLContract("invalid fixed sequence length")
      }
      sequenceShape = .fixed(defaultShape[1])
    @unknown default:
      throw LocalInferenceError.unsupportedCoreMLContract("unknown shape constraint")
    }

    let stateNames = Set(description.stateDescriptionsByName.keys)
    let camelState = Set(["keyCache", "valueCache"])
    let snakeState = Set(["key_cache", "value_cache"])
    let stateful = !stateNames.isEmpty
    if stateful {
      guard stateNames == camelState || stateNames == snakeState else {
        throw LocalInferenceError.unsupportedCoreMLContract(
          "stateful models require exactly key/value cache state"
        )
      }
      guard attentionNames.isEmpty else {
        throw LocalInferenceError.unsupportedCoreMLContract(
          "stateful models with additional attention-mask inputs are not yet supported safely"
        )
      }
    }

    return Contract(
      inputIdsName: inputIdNames[0],
      attentionMaskName: attentionNames.first,
      logitsName: "logits",
      sequenceShape: sequenceShape,
      stateful: stateful
    )
  }

  private static func sample(logits: MLTensor, tokenIndex: Int, temperature: Double) async throws -> Int {
    guard logits.rank == 3, logits.shape[0] == 1,
          tokenIndex >= 0, tokenIndex < logits.shape[1], logits.shape[2] > 0 else {
      throw LocalInferenceError.unsupportedCoreMLContract("logits must have shape [1, sequence, vocabulary]")
    }
    let row = logits[nil, tokenIndex, nil].flattened().cast(to: Float.self)
    let values = await row.shapedArray(of: Float.self).scalars
    guard !values.isEmpty, values.allSatisfy(\.isFinite) else {
      throw LocalInferenceError.inferenceFailed("the Core ML model produced invalid logits")
    }

    if temperature <= 0.0001 {
      return values.indices.max(by: { values[$0] < values[$1] }) ?? 0
    }

    let maximum = values.max() ?? 0
    let scaled = values.map { exp(Double($0 - maximum) / temperature) }
    let total = scaled.reduce(0, +)
    guard total.isFinite, total > 0 else {
      throw LocalInferenceError.inferenceFailed("the Core ML sampling distribution is invalid")
    }
    var threshold = Double.random(in: 0..<total)
    for (index, weight) in scaled.enumerated() {
      threshold -= weight
      if threshold <= 0 { return index }
    }
    return scaled.count - 1
  }
}
