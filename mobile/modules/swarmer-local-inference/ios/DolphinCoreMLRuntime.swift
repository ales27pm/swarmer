import Foundation
@preconcurrency import CoreML

enum DolphinCoreMLRuntimeError: LocalizedError, Sendable {
  case invalid(String)
  case busy

  var errorDescription: String? {
    switch self {
    case .invalid(let detail):
      return "Invalid Dolphin Core ML pipeline: \(detail)"
    case .busy:
      return "A Dolphin generation is already running."
    }
  }
}

struct DolphinCoreMLGeneration: Sendable {
  let generatedIds: [Int32]
  let finishReason: String
}

/// IEEE 754 binary16 helpers that do not depend on Swift.Float16 being available.
private enum DolphinBinary16 {
  static let zero: UInt16 = 0x0000
  static let one: UInt16 = 0x3c00
  static let negativeGreatestFiniteMagnitude: UInt16 = 0xfbff

  static func isFinite(_ bits: UInt16) -> Bool {
    bits & 0x7c00 != 0x7c00
  }

  static func float(from bits: UInt16) -> Float {
    let sign = UInt32(bits & 0x8000) << 16
    let exponent = Int((bits >> 10) & 0x001f)
    var fraction = UInt32(bits & 0x03ff)
    let floatBits: UInt32

    switch exponent {
    case 0 where fraction == 0:
      floatBits = sign
    case 0:
      var unbiasedExponent = -14
      while fraction & 0x0400 == 0 {
        fraction <<= 1
        unbiasedExponent -= 1
      }
      fraction &= 0x03ff
      floatBits = sign | (UInt32(unbiasedExponent + 127) << 23) | (fraction << 13)
    case 0x1f:
      floatBits = sign | 0x7f80_0000 | (fraction << 13)
    default:
      floatBits = sign | (UInt32(exponent - 15 + 127) << 23) | (fraction << 13)
    }
    return Float(bitPattern: floatBits)
  }

  static func bits(from bytes: UnsafeRawBufferPointer, elementOffset: Int) throws -> UInt16 {
    let byteOffset = elementOffset * MemoryLayout<UInt16>.size
    guard elementOffset >= 0, byteOffset + 1 < bytes.count else {
      throw DolphinCoreMLRuntimeError.invalid("truncated FP16 storage")
    }
    return UInt16(bytes[byteOffset]) | (UInt16(bytes[byteOffset + 1]) << 8)
  }

  static func multiArray(_ values: [UInt16], shape: [Int]) throws -> MLMultiArray {
    guard !shape.isEmpty, shape.allSatisfy({ $0 > 0 }), shape.reduce(1, *) == values.count else {
      throw DolphinCoreMLRuntimeError.invalid("invalid FP16 input shape")
    }
    let array = try MLMultiArray(
      shape: shape.map { NSNumber(value: $0) },
      dataType: .float16
    )
    try array.withUnsafeMutableBytes { bytes, strides in
      guard strides.count == shape.count else {
        throw DolphinCoreMLRuntimeError.invalid("invalid FP16 input strides")
      }
      var logicalStrides = [Int](repeating: 1, count: shape.count)
      if shape.count > 1 {
        for axis in stride(from: shape.count - 2, through: 0, by: -1) {
          logicalStrides[axis] = logicalStrides[axis + 1] * shape[axis + 1]
        }
      }
      for (linearIndex, value) in values.enumerated() {
        var elementOffset = 0
        for axis in shape.indices {
          let coordinate = (linearIndex / logicalStrides[axis]) % shape[axis]
          elementOffset += coordinate * strides[axis]
        }
        let byteOffset = elementOffset * MemoryLayout<UInt16>.size
        guard byteOffset + 1 < bytes.count else {
          throw DolphinCoreMLRuntimeError.invalid("invalid FP16 input storage")
        }
        bytes[byteOffset] = UInt8(truncatingIfNeeded: value)
        bytes[byteOffset + 1] = UInt8(truncatingIfNeeded: value >> 8)
      }
    }
    return array
  }
}

/// Runs the nine-model pipeline while keeping at most one Core ML model resident.
@available(iOS 18.0, macOS 15.0, *)
actor DolphinCoreMLRuntime {
  private struct BlockStateSnapshot {
    let keyCache: Data
    let valueCache: Data
  }

  private struct BlockPass {
    let hiddenStates: [MLMultiArray]
    let state: BlockStateSnapshot
  }

  private let manifest: DolphinCoreMLBundle.Manifest
  private let modelURLs: [URL]
  private let ropeCos: [UInt16]
  private let ropeSin: [UInt16]
  private var busy = false
  private var cancelRequested = false

  init(directory: URL) async throws {
    let bundle: DolphinCoreMLBundle
    do {
      bundle = try DolphinCoreMLBundle.inspect(directory: directory)
    } catch {
      throw DolphinCoreMLRuntimeError.invalid(error.localizedDescription)
    }

    var compiledModelURLs: [URL] = []
    compiledModelURLs.reserveCapacity(bundle.modelURLs.count)
    for modelURL in bundle.modelURLs {
      try Task.checkCancellation()
      if modelURL.pathExtension.lowercased() == "mlmodelc" {
        compiledModelURLs.append(modelURL)
      } else {
        compiledModelURLs.append(try await MLModel.compileModel(at: modelURL))
      }
    }
    try Task.checkCancellation()

    do {
      try await Self.validateModels(at: compiledModelURLs, manifest: bundle.manifest)
    } catch is CancellationError {
      throw CancellationError()
    } catch {
      try Task.checkCancellation()
      throw DolphinCoreMLRuntimeError.invalid(
        "CPU/Neural Engine model loading failed; no CPU/GPU retry was attempted "
          + "(\(error.localizedDescription))"
      )
    }
    manifest = bundle.manifest
    modelURLs = compiledModelURLs
    ropeCos = try Self.readRoPE(
      directory.appendingPathComponent("rope_cos.fp16"),
      count: bundle.manifest.contextLength * bundle.manifest.headDimension
    )
    ropeSin = try Self.readRoPE(
      directory.appendingPathComponent("rope_sin.fp16"),
      count: bundle.manifest.contextLength * bundle.manifest.headDimension
    )
  }

  private static func loadModel(at compiledModelURL: URL) async throws -> MLModel {
    let configuration = MLModelConfiguration()
    configuration.computeUnits = .cpuAndNeuralEngine
    try Task.checkCancellation()
    let model = try await MLModel.load(contentsOf: compiledModelURL, configuration: configuration)
    try Task.checkCancellation()
    return model
  }

  /// Cancellation leaves the lease held until every in-flight prediction has returned.
  func cancel() {
    if busy { cancelRequested = true }
  }

  func generate(
    tokens: [Int32],
    maxNewTokens: Int,
    temperature: Double
  ) async throws -> DolphinCoreMLGeneration {
    guard !busy else { throw DolphinCoreMLRuntimeError.busy }
    guard !tokens.isEmpty, maxNewTokens > 0,
          tokens.count <= manifest.contextLength,
          maxNewTokens - 1 <= manifest.contextLength - tokens.count,
          tokens.allSatisfy({ $0 >= 0 && Int($0) < manifest.vocabSize }),
          temperature.isFinite, temperature >= 0 else {
      throw DolphinCoreMLRuntimeError.invalid("token IDs, temperature, or context budget are invalid")
    }

    busy = true
    cancelRequested = false
    defer {
      busy = false
      cancelRequested = false
    }

    var hiddenStates = try await embed(tokens)
    var states: [BlockStateSnapshot] = []
    states.reserveCapacity(manifest.blockNames.count)
    for blockIndex in manifest.blockNames.indices {
      let pass = try await runBlock(
        blockIndex,
        hiddenStates: hiddenStates,
        startPosition: 0,
        restoring: nil
      )
      hiddenStates = pass.hiddenStates
      states.append(pass.state)
    }
    guard let finalPromptHidden = hiddenStates.last else {
      throw DolphinCoreMLRuntimeError.invalid("prefill produced no hidden state")
    }
    var nextToken = try DolphinCoreMLBundle.sample(
      logits: try await logits(for: finalPromptHidden),
      temperature: temperature
    )
    var consumed = tokens.count

    var generated: [Int32] = []
    generated.reserveCapacity(maxNewTokens)
    var finishReason = "length"
    for index in 0..<maxNewTokens {
      try checkCancellation()
      if manifest.eosTokenIds.contains(nextToken) {
        finishReason = "stop"
        break
      }
      generated.append(nextToken)
      if index + 1 == maxNewTokens { break }
      guard var hidden = try await embed([nextToken]).first else {
        throw DolphinCoreMLRuntimeError.invalid("decode produced no hidden state")
      }
      for blockIndex in manifest.blockNames.indices {
        let pass = try await runBlock(
          blockIndex,
          hiddenStates: [hidden],
          startPosition: consumed,
          restoring: states[blockIndex]
        )
        guard let blockHidden = pass.hiddenStates.first else {
          throw DolphinCoreMLRuntimeError.invalid("a block produced no hidden state")
        }
        hidden = blockHidden
        states[blockIndex] = pass.state
      }
      nextToken = try DolphinCoreMLBundle.sample(
        logits: try await logits(for: hidden),
        temperature: temperature
      )
      consumed += 1
    }
    try checkCancellation()
    return DolphinCoreMLGeneration(generatedIds: generated, finishReason: finishReason)
  }

  private func checkCancellation() throws {
    try Task.checkCancellation()
    if cancelRequested { throw CancellationError() }
  }

  private func embed(_ tokens: [Int32]) async throws -> [MLMultiArray] {
    try checkCancellation()
    let model = try await Self.loadModel(at: modelURLs[0])
    var hiddenStates: [MLMultiArray] = []
    hiddenStates.reserveCapacity(tokens.count)
    for token in tokens {
      let ids = try MLMultiArray(shape: [1, 1], dataType: .int32)
      ids[0] = NSNumber(value: token)
      let output = try await model.prediction(
        from: MLDictionaryFeatureProvider(dictionary: ["inputIds": ids])
      )
      try checkCancellation()
      hiddenStates.append(
        try Self.ownedOutput(output, name: "hiddenStates", width: manifest.hiddenSize)
      )
    }
    return hiddenStates
  }

  private func runBlock(
    _ blockIndex: Int,
    hiddenStates: [MLMultiArray],
    startPosition: Int,
    restoring snapshot: BlockStateSnapshot?
  ) async throws -> BlockPass {
    try checkCancellation()
    guard manifest.blockNames.indices.contains(blockIndex),
          !hiddenStates.isEmpty,
          startPosition >= 0,
          startPosition + hiddenStates.count <= manifest.contextLength else {
      throw DolphinCoreMLRuntimeError.invalid("the block pass exceeds the KV cache")
    }
    let model = try await Self.loadModel(at: modelURLs[blockIndex + 1])
    let state = model.makeState()
    if let snapshot {
      try Self.restore(snapshot, into: state, manifest: manifest)
    }

    var outputs: [MLMultiArray] = []
    outputs.reserveCapacity(hiddenStates.count)
    for (offset, hidden) in hiddenStates.enumerated() {
      let position = startPosition + offset
      let range = (position * manifest.headDimension)..<((position + 1) * manifest.headDimension)
      let cos = try Self.half(
        Array(ropeCos[range]),
        shape: [1, manifest.headDimension, 1, 1]
      )
      let sin = try Self.half(
        Array(ropeSin[range]),
        shape: [1, manifest.headDimension, 1, 1]
      )
      var writeMask = [UInt16](repeating: DolphinBinary16.zero, count: manifest.contextLength)
      writeMask[position] = DolphinBinary16.one
      let causalMask = (0..<manifest.contextLength).map {
        $0 <= position ? DolphinBinary16.zero : DolphinBinary16.negativeGreatestFiniteMagnitude
      }
      let inputs = try MLDictionaryFeatureProvider(dictionary: [
        "hiddenStates": hidden,
        "ropeCos": cos,
        "ropeSin": sin,
        "writeMask": try Self.half(
          writeMask,
          shape: [1, 1, 1, manifest.contextLength]
        ),
        "causalMask": try Self.half(
          causalMask,
          shape: [1, 1, 1, manifest.contextLength]
        ),
      ])
      let output = try await model.prediction(from: inputs, using: state)
      try checkCancellation()
      outputs.append(
        try Self.ownedOutput(
          output,
          name: "outputHiddenStates",
          width: manifest.hiddenSize
        )
      )
    }
    return BlockPass(
      hiddenStates: outputs,
      state: try Self.snapshot(of: state, manifest: manifest)
    )
  }

  private func logits(for hidden: MLMultiArray) async throws -> [Float] {
    try checkCancellation()
    let model = try await Self.loadModel(at: modelURLs[modelURLs.count - 1])
    let output = try await model.prediction(
      from: MLDictionaryFeatureProvider(dictionary: ["hiddenStates": hidden])
    )
    try checkCancellation()
    var logits: [Float] = []
    logits.reserveCapacity(manifest.vocabSize)
    for (head, width) in manifest.headSizes.enumerated() {
      let array = try Self.output(output, name: "logits_\(head)", width: width)
      try array.withUnsafeBytes { bytes in
        let elementStride = array.strides[1].intValue
        for index in 0..<width {
          let bits = try DolphinBinary16.bits(
            from: bytes,
            elementOffset: index * elementStride
          )
          let value = DolphinBinary16.float(from: bits)
          guard value.isFinite else {
            throw DolphinCoreMLRuntimeError.invalid("the model produced non-finite logits")
          }
          logits.append(value)
        }
      }
    }
    guard logits.count == manifest.vocabSize else {
      throw DolphinCoreMLRuntimeError.invalid("the vocabulary head has the wrong width")
    }
    return logits
  }

  private static func ownedOutput(
    _ provider: any MLFeatureProvider,
    name: String,
    width: Int
  ) throws -> MLMultiArray {
    let source = try output(provider, name: name, width: width)
    var values = [UInt16](repeating: DolphinBinary16.zero, count: width)
    try source.withUnsafeBytes { bytes in
      let elementStride = source.strides[1].intValue
      for index in 0..<width {
        values[index] = try DolphinBinary16.bits(
          from: bytes,
          elementOffset: index * elementStride
        )
      }
    }
    return try half(values, shape: [1, width, 1, 1])
  }

  private static func output(
    _ provider: any MLFeatureProvider,
    name: String,
    width: Int
  ) throws -> MLMultiArray {
    guard let array = provider.featureValue(for: name)?.multiArrayValue,
          array.dataType == .float16,
          array.shape.map(\.intValue) == [1, width, 1, 1] else {
      throw DolphinCoreMLRuntimeError.invalid("unexpected output \(name)")
    }
    return array
  }

  private static func snapshot(
    of state: MLState,
    manifest: DolphinCoreMLBundle.Manifest
  ) throws -> BlockStateSnapshot {
    let keyCache = try state.withMultiArray(for: "keyCache") {
      try stateData(from: $0, manifest: manifest)
    }
    let valueCache = try state.withMultiArray(for: "valueCache") {
      try stateData(from: $0, manifest: manifest)
    }
    return BlockStateSnapshot(keyCache: keyCache, valueCache: valueCache)
  }

  private static func restore(
    _ snapshot: BlockStateSnapshot,
    into state: MLState,
    manifest: DolphinCoreMLBundle.Manifest
  ) throws {
    try state.withMultiArray(for: "keyCache") {
      try restoreStateData(snapshot.keyCache, into: $0, manifest: manifest)
    }
    try state.withMultiArray(for: "valueCache") {
      try restoreStateData(snapshot.valueCache, into: $0, manifest: manifest)
    }
  }

  private static func stateData(
    from array: MLMultiArray,
    manifest: DolphinCoreMLBundle.Manifest
  ) throws -> Data {
    let layout = try stateLayout(array, manifest: manifest)
    var data = Data(count: layout.byteCount)
    try array.withUnsafeBytes { source in
      guard source.count >= layout.requiredStorageBytes,
            let sourceAddress = source.baseAddress else {
        throw DolphinCoreMLRuntimeError.invalid("the Core ML state buffer is truncated")
      }
      try data.withUnsafeMutableBytes { destination in
        guard destination.count == layout.byteCount,
              let destinationAddress = destination.baseAddress else {
          throw DolphinCoreMLRuntimeError.invalid("the saved Core ML state is empty")
        }
        if layout.strides == expectedStateStrides(manifest: manifest) {
          destinationAddress.copyMemory(from: sourceAddress, byteCount: layout.byteCount)
          return
        }
        try copyStateRows(
          from: source,
          sourceStrides: layout.strides,
          to: destination,
          manifest: manifest
        )
      }
    }
    return data
  }

  private static func restoreStateData(
    _ data: Data,
    into array: MLMultiArray,
    manifest: DolphinCoreMLBundle.Manifest
  ) throws {
    let layout = try stateLayout(array, manifest: manifest)
    guard data.count == layout.byteCount else {
      throw DolphinCoreMLRuntimeError.invalid("the saved Core ML state has the wrong byte length")
    }
    try array.withUnsafeMutableBytes { destination, strides in
      guard strides == layout.strides,
            destination.count >= layout.requiredStorageBytes,
            let destinationAddress = destination.baseAddress else {
        throw DolphinCoreMLRuntimeError.invalid("the Core ML state buffer is truncated")
      }
      try data.withUnsafeBytes { source in
        guard let sourceAddress = source.baseAddress else {
          throw DolphinCoreMLRuntimeError.invalid("the saved Core ML state is empty")
        }
        if layout.strides == expectedStateStrides(manifest: manifest) {
          destinationAddress.copyMemory(from: sourceAddress, byteCount: layout.byteCount)
          return
        }
        try copyStateRows(
          from: source,
          to: destination,
          destinationStrides: layout.strides,
          manifest: manifest
        )
      }
    }
  }

  private static func stateLayout(
    _ array: MLMultiArray,
    manifest: DolphinCoreMLBundle.Manifest
  ) throws -> (strides: [Int], byteCount: Int, requiredStorageBytes: Int) {
    let shape = [4, 1_024, 1, manifest.contextLength]
    let strides = array.strides.map(\.intValue)
    let elementCount = shape.reduce(1, *)
    guard array.dataType == .float16,
          array.shape.map(\.intValue) == shape,
          strides.count == shape.count,
          strides.allSatisfy({ $0 > 0 }) else {
      throw DolphinCoreMLRuntimeError.invalid("the Core ML state buffer has an invalid layout")
    }
    let maximumElementOffset = zip(shape, strides).reduce(0) {
      $0 + ($1.0 - 1) * $1.1
    }
    return (
      strides,
      elementCount * MemoryLayout<UInt16>.size,
      (maximumElementOffset + 1) * MemoryLayout<UInt16>.size
    )
  }

  private static func expectedStateStrides(
    manifest: DolphinCoreMLBundle.Manifest
  ) -> [Int] {
    [1_024 * manifest.contextLength, manifest.contextLength, manifest.contextLength, 1]
  }

  private static func copyStateRows(
    from source: UnsafeRawBufferPointer,
    sourceStrides: [Int],
    to destination: UnsafeMutableRawBufferPointer,
    manifest: DolphinCoreMLBundle.Manifest
  ) throws {
    guard let sourceAddress = source.baseAddress,
          let destinationAddress = destination.baseAddress else {
      throw DolphinCoreMLRuntimeError.invalid("the Core ML state buffer is empty")
    }
    let elementBytes = MemoryLayout<UInt16>.size
    let rowBytes = manifest.contextLength * elementBytes
    for layer in 0..<4 {
      for channel in 0..<1_024 {
        let sourceRow = layer * sourceStrides[0] + channel * sourceStrides[1]
        let destinationByte = (layer * 1_024 + channel) * rowBytes
        if sourceStrides[3] == 1 {
          destinationAddress.advanced(by: destinationByte).copyMemory(
            from: sourceAddress.advanced(by: sourceRow * elementBytes),
            byteCount: rowBytes
          )
          continue
        }
        for position in 0..<manifest.contextLength {
          destinationAddress.advanced(by: destinationByte + position * elementBytes).copyMemory(
            from: sourceAddress.advanced(
              by: (sourceRow + position * sourceStrides[3]) * elementBytes
            ),
            byteCount: elementBytes
          )
        }
      }
    }
  }

  private static func copyStateRows(
    from source: UnsafeRawBufferPointer,
    to destination: UnsafeMutableRawBufferPointer,
    destinationStrides: [Int],
    manifest: DolphinCoreMLBundle.Manifest
  ) throws {
    guard let sourceAddress = source.baseAddress,
          let destinationAddress = destination.baseAddress else {
      throw DolphinCoreMLRuntimeError.invalid("the Core ML state buffer is empty")
    }
    let elementBytes = MemoryLayout<UInt16>.size
    let rowBytes = manifest.contextLength * elementBytes
    for layer in 0..<4 {
      for channel in 0..<1_024 {
        let sourceByte = (layer * 1_024 + channel) * rowBytes
        let destinationRow = layer * destinationStrides[0] + channel * destinationStrides[1]
        if destinationStrides[3] == 1 {
          destinationAddress.advanced(by: destinationRow * elementBytes).copyMemory(
            from: sourceAddress.advanced(by: sourceByte),
            byteCount: rowBytes
          )
          continue
        }
        for position in 0..<manifest.contextLength {
          destinationAddress.advanced(
            by: (destinationRow + position * destinationStrides[3]) * elementBytes
          ).copyMemory(
            from: sourceAddress.advanced(by: sourceByte + position * elementBytes),
            byteCount: elementBytes
          )
        }
      }
    }
  }

  private typealias ArrayContract = (dataType: MLMultiArrayDataType, shape: [Int])

  /// Validates the executable Core ML signatures, not only the bundle manifest.
  /// Models are loaded and released one at a time so validation cannot recreate
  /// the multi-model Neural Engine residency failure this runtime avoids.
  private static func validateModels(
    at modelURLs: [URL],
    manifest: DolphinCoreMLBundle.Manifest
  ) async throws {
    guard modelURLs.count == DolphinCoreMLBundle.expectedModelStems.count else {
      throw DolphinCoreMLRuntimeError.invalid("the pipeline must contain exactly nine models")
    }
    for index in modelURLs.indices {
      try await validateModel(at: modelURLs[index], index: index, manifest: manifest)
    }
  }

  private static func validateModel(
    at modelURL: URL,
    index: Int,
    manifest: DolphinCoreMLBundle.Manifest
  ) async throws {
    let model = try await loadModel(at: modelURL)
    let description = model.modelDescription
    if index == 0 {
      try requireMultiArrays(
        description.inputDescriptionsByName,
        expected: ["inputIds": (.int32, [1, 1])],
        label: "embedding inputs"
      )
      try requireMultiArrays(
        description.outputDescriptionsByName,
        expected: ["hiddenStates": (.float16, [1, manifest.hiddenSize, 1, 1])],
        label: "embedding outputs"
      )
      try requireNoStates(description, label: "embedding")
      return
    }

    if index == modelURLsLastIndex {
      try requireMultiArrays(
        description.inputDescriptionsByName,
        expected: ["hiddenStates": (.float16, [1, manifest.hiddenSize, 1, 1])],
        label: "head inputs"
      )
      let headOutputs = Dictionary(uniqueKeysWithValues: manifest.headSizes.enumerated().map {
        ("logits_\($0.offset)", ArrayContract(.float16, [1, $0.element, 1, 1]))
      })
      try requireMultiArrays(
        description.outputDescriptionsByName,
        expected: headOutputs,
        label: "head outputs"
      )
      try requireNoStates(description, label: "head")
      return
    }

    let blockIndex = index - 1
    guard manifest.blockNames.indices.contains(blockIndex) else {
      throw DolphinCoreMLRuntimeError.invalid("the pipeline contains an unexpected model")
    }
    let label = manifest.blockNames[blockIndex]
    let blockInputs: [String: ArrayContract] = [
      "hiddenStates": (.float16, [1, manifest.hiddenSize, 1, 1]),
      "ropeCos": (.float16, [1, manifest.headDimension, 1, 1]),
      "ropeSin": (.float16, [1, manifest.headDimension, 1, 1]),
      "writeMask": (.float16, [1, 1, 1, manifest.contextLength]),
      "causalMask": (.float16, [1, 1, 1, manifest.contextLength]),
    ]
    let blockOutputs: [String: ArrayContract] = [
      "outputHiddenStates": (.float16, [1, manifest.hiddenSize, 1, 1]),
    ]
    let blockStates: [String: ArrayContract] = [
      "keyCache": (.float16, [4, 1_024, 1, manifest.contextLength]),
      "valueCache": (.float16, [4, 1_024, 1, manifest.contextLength]),
    ]
    try requireMultiArrays(
      description.inputDescriptionsByName,
      expected: blockInputs,
      label: "\(label) inputs"
    )
    try requireMultiArrays(
      description.outputDescriptionsByName,
      expected: blockOutputs,
      label: "\(label) outputs"
    )
    try requireStates(description, expected: blockStates, label: "\(label) states")
  }

  private static let modelURLsLastIndex = DolphinCoreMLBundle.expectedModelStems.count - 1

  private static func requireMultiArrays(
    _ descriptions: [String: MLFeatureDescription],
    expected: [String: ArrayContract],
    label: String
  ) throws {
    guard Set(descriptions.keys) == Set(expected.keys) else {
      throw DolphinCoreMLRuntimeError.invalid("unexpected feature names in \(label)")
    }
    for (name, contract) in expected {
      guard let description = descriptions[name],
            description.type == .multiArray,
            description.isOptional == false,
            let constraint = description.multiArrayConstraint,
            constraint.dataType == contract.dataType,
            constraint.shape.map(\.intValue) == contract.shape else {
        throw DolphinCoreMLRuntimeError.invalid("unexpected feature \(name) in \(label)")
      }
    }
  }

  private static func requireStates(
    _ description: MLModelDescription,
    expected: [String: ArrayContract],
    label: String
  ) throws {
    let states = description.stateDescriptionsByName
    guard Set(states.keys) == Set(expected.keys) else {
      throw DolphinCoreMLRuntimeError.invalid("unexpected feature names in \(label)")
    }
    for (name, contract) in expected {
      guard let feature = states[name],
            feature.type == .state,
            feature.isOptional == false,
            let constraint = feature.stateConstraint,
            constraint.dataType == contract.dataType,
            constraint.bufferShape == contract.shape else {
        throw DolphinCoreMLRuntimeError.invalid("unexpected feature \(name) in \(label)")
      }
    }
  }

  private static func requireNoStates(
    _ description: MLModelDescription,
    label: String
  ) throws {
    guard description.stateDescriptionsByName.isEmpty else {
      throw DolphinCoreMLRuntimeError.invalid("unexpected state features in \(label)")
    }
  }

  private static func half(_ values: [UInt16], shape: [Int]) throws -> MLMultiArray {
    try DolphinBinary16.multiArray(values, shape: shape)
  }

  private static func readRoPE(_ url: URL, count: Int) throws -> [UInt16] {
    let data = try Data(contentsOf: url)
    guard data.count == count * MemoryLayout<UInt16>.size else {
      throw DolphinCoreMLRuntimeError.invalid("invalid RoPE table length")
    }
    let values = (0..<count).map {
      UInt16(data[$0 * 2]) | (UInt16(data[$0 * 2 + 1]) << 8)
    }
    guard values.allSatisfy(DolphinBinary16.isFinite) else {
      throw DolphinCoreMLRuntimeError.invalid("the RoPE tables contain non-finite values")
    }
    return values
  }
}
