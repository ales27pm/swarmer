import CoreML
import Foundation
import Tokenizers
#if DEBUG
import CryptoKit
#endif

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
    let dolphinCausalMask: Bool

    var contextLength: Int {
      dolphinCausalMask ? CoreMLDolphinSupport.contextLength : sequenceShape.maximum
    }
  }

  private var model: LoadedModel?
  private var tokenizer: (any Tokenizer)?
  private var contract: Contract?
  private var stopTokenIDs = Set<Int>()
  private var cancelRequested = false
  private var generating = false

  func load(
    modelURL: URL, tokenizerURL: URL, diagnosticUnits: CoreMLDiagnosticComputeUnits? = nil
  ) async throws -> CoreMLLoadDiagnostic? {
    let units = try CoreMLDiagnosticComputeUnits.requested(diagnosticUnits?.rawValue, runtime: "coreml")
    guard !generating else { throw LocalInferenceError.generationInProgress }
    let startedAt = ProcessInfo.processInfo.systemUptime
    var stage = CoreMLLoadDiagnostic.Stage.validateArtifact
    do {
      cancelRequested = false
      let ext = modelURL.pathExtension.lowercased()
      guard ["mlmodel", "mlpackage", "mlmodelc"].contains(ext) else {
        throw LocalInferenceError.unsupportedModel("unsupported Core ML extension")
      }

      let compiledURL: URL
      if ext == "mlmodelc" {
        compiledURL = modelURL
      } else {
        stage = .compile
        compiledURL = try await MLModel.compileModel(at: modelURL)
      }
      try checkCancellation()

      let configuration = MLModelConfiguration()
      switch units ?? .all {
      case .all: configuration.computeUnits = .all
      case .cpuOnly: configuration.computeUnits = .cpuOnly
      case .cpuAndGPU: configuration.computeUnits = .cpuAndGPU
      case .cpuAndNeuralEngine: configuration.computeUnits = .cpuAndNeuralEngine
      }
      stage = .load
      let loadedModel = try await MLModel.load(contentsOf: compiledURL, configuration: configuration)
      try checkCancellation()
      stage = .validateContract
      let loadedContract = try Self.validate(model: loadedModel)
      stage = .tokenizer
      let loadedTokenizer = try await AutoTokenizer.from(modelFolder: tokenizerURL, strict: true)
      var loadedStopTokenIDs = try CoreMLDolphinSupport.configuredStopTokenIDs(in: tokenizerURL)
      if let eosTokenID = loadedTokenizer.eosTokenId { loadedStopTokenIDs.insert(eosTokenID) }
      if loadedContract.dolphinCausalMask {
        loadedStopTokenIDs.formUnion(CoreMLDolphinSupport.stopTokenIDs)
      }
      try checkCancellation()

      stopTokenIDs = loadedStopTokenIDs
      model = LoadedModel(loadedModel)
      contract = loadedContract
      tokenizer = loadedTokenizer
      cancelRequested = false
      return units.map { .capture(units: $0, stage: .complete, startedAt: startedAt) }
    } catch {
      guard let units else { throw error }
      throw CoreMLDiagnosticLoadFailure(diagnostic: .capture(
        units: units, stage: stage, startedAt: startedAt, error: error
      ))
    }
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

    var allTokens = contract.dolphinCausalMask
      ? try tokenizer.applyChatTemplate(messages: [["role": "user", "content": prompt]])
      : tokenizer.encode(text: prompt, addSpecialTokens: true)
    guard !allTokens.isEmpty, allTokens.allSatisfy({ Int32(exactly: $0) != nil }) else {
      throw LocalInferenceError.inferenceFailed("the tokenizer returned invalid token IDs")
    }
    guard maxTokens > 0, allTokens.count <= contract.contextLength,
          maxTokens <= contract.contextLength - allTokens.count else {
      throw LocalInferenceError.contextExceeded
    }
    if contract.stateful && !contract.dolphinCausalMask {
      guard contract.sequenceShape.accepts(allTokens.count), contract.sequenceShape.accepts(1) else {
        throw LocalInferenceError.unsupportedCoreMLContract(
          "stateful models must accept both the complete prompt length and one-token extension inputs"
        )
      }
    }

    let prefillRanges = contract.dolphinCausalMask
      ? try CoreMLDolphinSupport.prefillRanges(tokenCount: allTokens.count, maxNewTokens: maxTokens)
      : [0..<allTokens.count]
    let generationStopTokenIDs = stopTokenIDs
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
      // Only the last prefill chunk predicts a response token. Every chunk uses
      // the same fresh conversation state and its absolute end position.
      let chunks = step == 0 ? prefillRanges : [0..<predictionTokens.count]
      var finalLogits: MLTensor?
      var finalTokenIndex = 0
      for chunk in chunks {
        let queryTokens = Array(predictionTokens[chunk])
        let targetLength = contract.stateful
          ? queryTokens.count
          : try contract.sequenceShape.paddedLength(for: queryTokens.count)
        let paddingCount = targetLength - queryTokens.count
        let paddedTokens = queryTokens.map(Int32.init) + [Int32](repeating: 0, count: paddingCount)
        var inputs = [
          contract.inputIdsName: MLTensor(shape: [1, targetLength], scalars: paddedTokens)
        ]
        if let attentionMaskName = contract.attentionMaskName {
          let mask = [Int32](repeating: 1, count: queryTokens.count)
            + [Int32](repeating: 0, count: paddingCount)
          inputs[attentionMaskName] = MLTensor(shape: [1, targetLength], scalars: mask)
        }
        if contract.dolphinCausalMask {
          let absoluteEnd = step == 0 ? chunk.upperBound : allTokens.count + step
          let mask = try CoreMLDolphinSupport.causalMask(
            queryCount: queryTokens.count, absoluteEnd: absoluteEnd
          )
          #if arch(arm64)
          inputs["causalMask"] = MLTensor(
            shape: [1, 1, queryTokens.count, absoluteEnd], scalars: mask.map(Float16.init(bitPattern:))
          )
          #else
          throw LocalInferenceError.unsupportedModel("Dolphin Core ML requires an Apple-silicon device")
          #endif
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
        if contract.dolphinCausalMask {
          guard logits.rank == 3, logits.shape == [1, queryTokens.count, CoreMLDolphinSupport.vocabularySize] else {
            throw LocalInferenceError.unsupportedCoreMLContract("Dolphin returned an unexpected logits shape")
          }
        }
        finalLogits = logits
        finalTokenIndex = queryTokens.count - 1
      }
      guard let finalLogits else {
        throw LocalInferenceError.inferenceFailed("the Core ML prompt produced no predictions")
      }
      let nextToken = try await Self.sample(
        logits: finalLogits,
        tokenIndex: finalTokenIndex,
        temperature: temperature
      )
      if generationStopTokenIDs.contains(nextToken) {
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
    stopTokenIDs.removeAll()
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

    let dolphinCausalMask = inputs["causalMask"] != nil
    let allowedInputs = Set(inputIdNames + attentionNames + (dolphinCausalMask ? ["causalMask"] : []))
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
    guard (dolphinCausalMask && logitsShape.isEmpty)
      || (logitsShape.count == 3 && logitsShape[0] == 1) else {
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

    if dolphinCausalMask {
      guard inputIdNames == ["inputIds"], attentionNames.isEmpty,
            stateNames == camelState,
            logitsConstraint.dataType == .float16,
            (logitsShape.isEmpty || logitsShape[2] == CoreMLDolphinSupport.vocabularySize),
            matchesRanges(inputConstraint, bounds: [1...1, 1...CoreMLDolphinSupport.queryLength]),
            let mask = inputs["causalMask"]?.multiArrayConstraint,
            mask.dataType == .float16,
            matchesRanges(mask, bounds: [1...1, 1...1, 1...CoreMLDolphinSupport.queryLength,
                                         1...CoreMLDolphinSupport.contextLength]) else {
        throw LocalInferenceError.unsupportedCoreMLContract(
          "Dolphin requires Int32 inputIds, a dynamic FP16 causalMask, and FP16 vocabulary logits"
        )
      }
      for name in camelState {
        guard let state = description.stateDescriptionsByName[name]?.stateConstraint,
              state.dataType == .float16,
              state.bufferShape == CoreMLDolphinSupport.cacheShape else {
          throw LocalInferenceError.unsupportedCoreMLContract(
            "Dolphin requires FP16 keyCache/valueCache shaped [28, 1, 8, 2048, 128]"
          )
        }
      }
    }

    return Contract(
      inputIdsName: inputIdNames[0],
      attentionMaskName: attentionNames.first,
      logitsName: "logits",
      sequenceShape: sequenceShape,
      stateful: stateful,
      dolphinCausalMask: dolphinCausalMask
    )
  }

  private static func matchesRanges(
    _ constraint: MLMultiArrayConstraint,
    bounds: [ClosedRange<Int>]
  ) -> Bool {
    guard constraint.shape.count == bounds.count,
          constraint.shapeConstraint.type == .range else { return false }
    let ranges = constraint.shapeConstraint.sizeRangeForDimension
    guard ranges.count == bounds.count else { return false }
    return zip(ranges, bounds).allSatisfy { value, expected in
      let range = value.rangeValue
      return range.location == expected.lowerBound
        && range.length == expected.upperBound - expected.lowerBound + 1
    }
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

#if DEBUG
// Small, immutable app resources only. Device preferences describe Core ML's
// compute plan; they are not measurements of hardware execution.
@available(iOS 18.0, *)
enum CoreMLFixtureProbe {
  static let fixtureIDs = CoreMLProbeCatalog.fixtureIDs
  static let timeoutSeconds: UInt64 = 90
  private static let maximumElements = 1_000_000
  private static let maximumPackageBytes = 32 * 1024 * 1024

  // Share resource admission only. The direct loader does not use the fixture
  // execution session, async loader, compute plan, state or prediction path.
  static func verifiedPackage(fixtureID: String) throws -> URL {
    guard fixtureIDs.contains(fixtureID),
          let root = Bundle.main.url(forResource: "CoreMLProbeFixtures", withExtension: nil) else {
      throw invalidResource()
    }
    let rootValues = try root.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
    guard rootValues.isDirectory == true, rootValues.isSymbolicLink == false else { throw invalidResource() }
    let manifestURL = try resourceURL("manifest.json", root: root, directory: false)
    let manifest = try JSONDecoder().decode(Manifest.self, from: boundedData(manifestURL, maximumBytes: 128 * 1024))
    guard manifest.schemaVersion == 1, CoreMLProbeCatalog.accepts(manifest.fixtures.map(\.id)),
          let fixture = manifest.fixtures.first(where: { $0.id == fixtureID }),
          (1...3).contains(fixture.steps.count),
          fixture.absoluteTolerance.isFinite, (0...0.1).contains(fixture.absoluteTolerance),
          fixture.relativeTolerance.isFinite, (0...0.1).contains(fixture.relativeTolerance) else {
      throw invalidResource()
    }
    let package = try resourceURL(fixture.modelPath, root: root, directory: true)
    guard package.pathExtension == "mlpackage",
          package.deletingPathExtension().lastPathComponent == fixtureID else { throw invalidResource() }
    try verifyPackage(package, files: fixture.modelFiles)
    // Retain the existing bounded tensor/hash admission before compilation.
    var totalElements = 0
    for step in fixture.steps {
      guard (1...16).contains(step.inputs.count), (1...4).contains(step.outputs.count),
            Set(step.inputs.map(\.name)).count == step.inputs.count,
            Set(step.outputs.map(\.name)).count == step.outputs.count else { throw invalidResource() }
      for input in step.inputs {
        guard input.dtype == "float16" || input.dtype == "int32" else { throw invalidResource() }
      }
      for tensor in step.inputs + step.outputs {
        totalElements += try tensorValues(tensor, root: root).count
        guard totalElements <= maximumElements else { throw invalidResource() }
      }
    }
    return package
  }

  private struct Manifest: Decodable {
    let schemaVersion: Int
    let fixtures: [Fixture]
  }
  private struct FileReference: Decodable {
    let path: String
    let sha256: String
  }
  private struct TensorReference: Decodable {
    let name: String
    let shape: [Int]
    let dtype: String?
    let path: String
    let sha256: String
  }
  private struct Step: Decodable {
    let label: String
    let resetState: Bool
    let inputs: [TensorReference]
    let outputs: [TensorReference]
  }
  private struct Fixture: Decodable {
    let id: String
    let modelPath: String
    let modelFiles: [FileReference]
    let stateful: Bool
    let steps: [Step]
    let absoluteTolerance: Double
    let relativeTolerance: Double
  }
  struct DeviceCounts: Codable, Sendable {
    var cpu = 0
    var gpu = 0
    var neuralEngine = 0
    var unknown = 0

    mutating func add(_ device: MLComputeDevice?) {
      switch device {
      case .cpu?: cpu += 1
      case .gpu?: gpu += 1
      case .neuralEngine?: neuralEngine += 1
      case nil: unknown += 1
      @unknown default: unknown += 1
      }
    }
  }
  struct Report: Encodable, Sendable {
    let schemaVersion = 1
    let fixtureID: String
    let computeUnits: String
    var outcome = "failed"
    var stage = "resolve"
    var loadMilliseconds: Double = 0
    var predictionMilliseconds: Double = 0
    var preferredDeviceCounts = DeviceCounts()
    var supportedDeviceCounts = DeviceCounts()
    var maxAbsoluteError: Double?
    var elementsCompared = 0
    var errors: [CoreMLLoadDiagnostic.Failure] = []
    let hardwareExecutionMeasured = false

    private enum CodingKeys: String, CodingKey {
      case schemaVersion, fixtureID, computeUnits, outcome, stage
      case loadMilliseconds, predictionMilliseconds, preferredDeviceCounts, supportedDeviceCounts
      case maxAbsoluteError, elementsCompared, errors, hardwareExecutionMeasured
    }
    func encode(to encoder: any Encoder) throws {
      var container = encoder.container(keyedBy: CodingKeys.self)
      try container.encode(schemaVersion, forKey: .schemaVersion)
      try container.encode(fixtureID, forKey: .fixtureID)
      try container.encode(computeUnits, forKey: .computeUnits)
      try container.encode(outcome, forKey: .outcome)
      try container.encode(stage, forKey: .stage)
      try container.encode(loadMilliseconds, forKey: .loadMilliseconds)
      try container.encode(predictionMilliseconds, forKey: .predictionMilliseconds)
      try container.encode(preferredDeviceCounts, forKey: .preferredDeviceCounts)
      try container.encode(supportedDeviceCounts, forKey: .supportedDeviceCounts)
      if let maxAbsoluteError { try container.encode(maxAbsoluteError, forKey: .maxAbsoluteError) }
      else { try container.encodeNil(forKey: .maxAbsoluteError) }
      try container.encode(elementsCompared, forKey: .elementsCompared)
      try container.encode(errors, forKey: .errors)
      try container.encode(hardwareExecutionMeasured, forKey: .hardwareExecutionMeasured)
    }
    func json() throws -> String {
      String(decoding: try JSONEncoder().encode(self), as: UTF8.self)
    }
  }

  // Core ML owns these objects. This wrapper stays within one probe task, which
  // performs serial predictions and never shares a state with another request.
  private final class PredictionSession: @unchecked Sendable {
    let model: MLModel
    var state: MLState?
    init(model: MLModel, stateful: Bool) {
      self.model = model
      state = stateful ? model.makeState() : nil
    }
    func reset() { state = model.makeState() }
    func predict(_ inputs: [String: MLTensor]) async throws -> [String: MLTensor] {
      if let state { return try await model.prediction(from: inputs, using: state) }
      return try await model.prediction(from: inputs)
    }
  }

  static func run(fixtureID: String, units: CoreMLDiagnosticComputeUnits) async -> Report {
    var report = Report(fixtureID: fixtureID, computeUnits: units.rawValue)
    let startedAt = ProcessInfo.processInfo.systemUptime
    var phaseStarted = startedAt
    var compiledURL: URL?
    defer { if let compiledURL { try? FileManager.default.removeItem(at: compiledURL) } }
    do {
      try checkpoint(startedAt)
      guard fixtureIDs.contains(fixtureID),
            let root = Bundle.main.url(forResource: "CoreMLProbeFixtures", withExtension: nil) else {
        throw invalidResource()
      }
      let rootValues = try root.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
      guard rootValues.isDirectory == true, rootValues.isSymbolicLink == false else { throw invalidResource() }
      let manifestURL = try resourceURL("manifest.json", root: root, directory: false)
      let manifestData = try boundedData(manifestURL, maximumBytes: 128 * 1024)
      let manifest = try JSONDecoder().decode(Manifest.self, from: manifestData)
      guard manifest.schemaVersion == 1, CoreMLProbeCatalog.accepts(manifest.fixtures.map(\.id)),
            let fixture = manifest.fixtures.first(where: { $0.id == fixtureID }),
            (1...3).contains(fixture.steps.count),
            fixture.absoluteTolerance.isFinite, (0...0.1).contains(fixture.absoluteTolerance),
            fixture.relativeTolerance.isFinite, (0...0.1).contains(fixture.relativeTolerance) else {
        throw invalidResource()
      }
      let package = try resourceURL(fixture.modelPath, root: root, directory: true)
      guard package.pathExtension == "mlpackage" else { throw invalidResource() }
      try verifyPackage(package, files: fixture.modelFiles)
      // Validate every tensor before asking Core ML to compile anything.
      var totalElements = 0
      for step in fixture.steps {
        guard (1...16).contains(step.inputs.count), (1...4).contains(step.outputs.count),
              Set(step.inputs.map(\.name)).count == step.inputs.count,
              Set(step.outputs.map(\.name)).count == step.outputs.count else { throw invalidResource() }
        for input in step.inputs {
          guard input.dtype == "float16" || input.dtype == "int32" else { throw invalidResource() }
        }
        for tensor in step.inputs + step.outputs {
          let values = try tensorValues(tensor, root: root)
          totalElements += values.count
          guard totalElements <= maximumElements else { throw invalidResource() }
        }
      }
      try checkpoint(startedAt)
      report.stage = "compile"
      let compiled = try await MLModel.compileModel(at: package)
      compiledURL = compiled
      try checkpoint(startedAt)
      let configuration = MLModelConfiguration()
      switch units {
      case .all: configuration.computeUnits = .all
      case .cpuOnly: configuration.computeUnits = .cpuOnly
      case .cpuAndGPU: configuration.computeUnits = .cpuAndGPU
      case .cpuAndNeuralEngine: configuration.computeUnits = .cpuAndNeuralEngine
      }
      report.stage = "load"
      phaseStarted = ProcessInfo.processInfo.systemUptime
      let model = try await MLModel.load(contentsOf: compiled, configuration: configuration)
      report.loadMilliseconds = elapsed(since: phaseStarted)
      try checkpoint(startedAt)
      let session = PredictionSession(model: model, stateful: fixture.stateful)
      report.stage = "plan"
      let plan = try await MLComputePlan.load(contentsOf: compiled, configuration: configuration)
      var operationCount = 0
      try countDevices(plan.modelStructure, plan: plan, report: &report, count: &operationCount)
      guard operationCount > 0 else { throw invalidResource() }
      try checkpoint(startedAt)
      var matches = true
      for step in fixture.steps {
        if fixture.stateful && step.resetState { session.reset() }
        var inputs: [String: MLTensor] = [:]
        for input in step.inputs {
          let values = try tensorValues(input, root: root)
          if input.dtype == "int32" {
            guard values.allSatisfy({ $0.rounded() == $0 && Double($0) >= Double(Int32.min)
              && Double($0) <= Double(Int32.max) }) else { throw invalidResource() }
            inputs[input.name] = MLTensor(shape: input.shape, scalars: values.map { Int32($0) })
          } else {
            let half = values.map { Float16($0) }
            guard half.allSatisfy(\.isFinite) else { throw invalidResource() }
            inputs[input.name] = MLTensor(shape: input.shape, scalars: half)
          }
        }
        report.stage = "predict"
        phaseStarted = ProcessInfo.processInfo.systemUptime
        let outputs = try await session.predict(inputs)
        report.predictionMilliseconds += elapsed(since: phaseStarted)
        try checkpoint(startedAt)
        report.stage = "compare"
        for expected in step.outputs {
          guard let output = outputs[expected.name], output.shape == expected.shape else { throw invalidResource() }
          let actual = await output.cast(to: Float.self).shapedArray(of: Float.self).scalars
          let reference = try tensorValues(expected, root: root)
          guard actual.count == reference.count, actual.allSatisfy(\.isFinite) else { throw invalidResource() }
          for (value, target) in zip(actual, reference) {
            let error = abs(Double(value) - Double(target))
            report.maxAbsoluteError = max(report.maxAbsoluteError ?? 0, error)
            if error > fixture.absoluteTolerance + fixture.relativeTolerance * abs(Double(target)) { matches = false }
          }
          report.elementsCompared += reference.count
        }
        try checkpoint(startedAt)
      }
      if matches && report.elementsCompared > 0 {
        report.outcome = "passed"
        report.stage = "complete"
      }
    } catch {
      if report.stage == "load" { report.loadMilliseconds = elapsed(since: phaseStarted) }
      if report.stage == "predict" { report.predictionMilliseconds += elapsed(since: phaseStarted) }
      let cancelled = error is CancellationError || Task.isCancelled
      report.outcome = cancelled ? "cancelled" : "failed"
      let safeError: any Error = cancelled
        ? NSError(domain: NSCocoaErrorDomain, code: NSUserCancelledError) : error
      report.errors = CoreMLLoadDiagnostic.capture(
        units: units, stage: .load, startedAt: startedAt, error: safeError
      ).errors
    }
    return report
  }

  private static func elapsed(since start: TimeInterval) -> Double {
    let value = (ProcessInfo.processInfo.systemUptime - start) * 1000
    return value.isFinite ? min(86_400_000, max(0, value)) : 0
  }
  private static func checkpoint(_ start: TimeInterval) throws {
    try Task.checkCancellation()
    guard ProcessInfo.processInfo.systemUptime - start < Double(timeoutSeconds) else { throw CancellationError() }
  }
  private static func invalidResource() -> NSError {
    NSError(domain: NSCocoaErrorDomain, code: NSFileReadCorruptFileError)
  }
  private static func resourceURL(_ relative: String, root: URL, directory: Bool) throws -> URL {
    let parts = relative.split(separator: "/", omittingEmptySubsequences: false)
    guard !parts.isEmpty, relative.utf8.count <= 512, parts.count <= 16,
          parts.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".."
            && $0.range(of: #"^[A-Za-z0-9_.-]+$"#, options: .regularExpression) != nil }) else {
      throw invalidResource()
    }
    var url = root
    for part in parts {
      url.appendPathComponent(String(part))
      guard try url.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink == false else { throw invalidResource() }
    }
    let values = try url.resourceValues(forKeys: [.isDirectoryKey, .isRegularFileKey])
    guard directory ? values.isDirectory == true : values.isRegularFile == true else { throw invalidResource() }
    return url
  }
  private static func boundedData(_ url: URL, maximumBytes: Int) throws -> Data {
    let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? -1
    guard size >= 0, size <= maximumBytes else { throw invalidResource() }
    let data = try Data(contentsOf: url)
    guard data.count == size else { throw invalidResource() }
    return data
  }
  private static func checkedData(_ url: URL, hash: String, maximumBytes: Int) throws -> Data {
    guard hash.range(of: #"^[0-9a-f]{64}$"#, options: .regularExpression) != nil else { throw invalidResource() }
    let data = try boundedData(url, maximumBytes: maximumBytes)
    guard SHA256.hash(data: data).map({ String(format: "%02x", $0) }).joined() == hash else { throw invalidResource() }
    return data
  }
  private static func tensorValues(_ tensor: TensorReference, root: URL) throws -> [Float] {
    guard !tensor.name.isEmpty, tensor.name.utf8.count <= 128, (1...6).contains(tensor.shape.count) else { throw invalidResource() }
    var count = 1
    for dimension in tensor.shape {
      guard dimension > 0, dimension <= maximumElements, count <= maximumElements / dimension else { throw invalidResource() }
      count *= dimension
    }
    let data = try checkedData(resourceURL(tensor.path, root: root, directory: false), hash: tensor.sha256,
                               maximumBytes: maximumElements * 4)
    guard data.count == count * 4 else { throw invalidResource() }
    let values: [Float] = data.withUnsafeBytes { bytes in
      (0..<count).map { Float(bitPattern: UInt32(littleEndian: bytes.loadUnaligned(fromByteOffset: $0 * 4, as: UInt32.self))) }
    }
    guard values.allSatisfy(\.isFinite) else { throw invalidResource() }
    return values
  }
  private static func verifyPackage(_ package: URL, files: [FileReference]) throws {
    guard (1...512).contains(files.count), Set(files.map(\.path)).count == files.count,
          let enumerator = FileManager.default.enumerator(at: package,
            includingPropertiesForKeys: [.isSymbolicLinkKey, .isRegularFileKey, .isDirectoryKey]) else { throw invalidResource() }
    var actual = Set<String>()
    var entries = 0
    for case let url as URL in enumerator {
      entries += 1
      let values = try url.resourceValues(forKeys: [.isSymbolicLinkKey, .isRegularFileKey, .isDirectoryKey])
      guard entries <= 1024, values.isSymbolicLink == false,
            values.isRegularFile == true || values.isDirectory == true else { throw invalidResource() }
      if values.isRegularFile == true { actual.insert(String(url.path.dropFirst(package.path.count + 1))) }
    }
    guard actual == Set(files.map(\.path)) else { throw invalidResource() }
    var total = 0
    for file in files {
      let data = try checkedData(resourceURL(file.path, root: package, directory: false), hash: file.sha256,
                                 maximumBytes: maximumPackageBytes - total)
      total += data.count
    }
  }
  private static func countDevices(_ structure: MLModelStructure, plan: MLComputePlan,
                                   report: inout Report, count: inout Int) throws {
    // The fixed fixtures are ML Programs. Do not silently report zero placement
    // evidence for an unexpected format.
    guard case .program(let program) = structure else { throw invalidResource() }
    for function in program.functions.values {
      try countBlock(function.block, plan: plan, report: &report, count: &count, depth: 0)
    }
  }
  private static func countBlock(_ block: MLModelStructure.Program.Block, plan: MLComputePlan,
                                report: inout Report, count: inout Int, depth: Int) throws {
    guard depth <= 16 else { throw invalidResource() }
    for operation in block.operations {
      count += 1
      guard count <= 100_000 else { throw invalidResource() }
      let usage = plan.deviceUsage(for: operation)
      report.preferredDeviceCounts.add(usage?.preferred)
      if let usage {
        guard usage.supported.count <= 10 else { throw invalidResource() }
        for device in usage.supported { report.supportedDeviceCounts.add(device) }
      } else { report.supportedDeviceCounts.add(nil) }
      for nested in operation.blocks {
        try countBlock(nested, plan: plan, report: &report, count: &count, depth: depth + 1)
      }
    }
  }
}
#endif
