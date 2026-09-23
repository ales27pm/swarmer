import Foundation
import HuggingFace
import MLX
import MLXHuggingFace
import MLXLLM
import MLXLMCommon
import MLXNN
import Tokenizers
#if DEBUG
import os
#endif

/// The factory still owns model creation; its tokenizer child only retrieves the
/// already prepared value, so tokenizer JSON parsing cannot overlap weight load.
private struct PreloadedMLXTokenizerLoader: MLXLMCommon.TokenizerLoader {
  let directory: URL
  let tokenizer: any MLXLMCommon.Tokenizer

  func load(from directory: URL) async throws -> any MLXLMCommon.Tokenizer {
    guard directory.standardizedFileURL == self.directory.standardizedFileURL else {
      throw LocalInferenceError.unsupportedModel("the tokenizer directory changed during model loading")
    }
    try Task.checkCancellation()
    return tokenizer
  }
}

/// Counts completed iterator steps independently of detokenized text. A special
/// token can complete real work without producing a visible chunk.
private struct WorkReportingTokenIterator: TokenIteratorProtocol {
  var base: TokenIterator
  let onWorkProgress: (@Sendable (Int) async -> Void)?
  private var completedSteps = 0

  init(base: TokenIterator, onWorkProgress: (@Sendable (Int) async -> Void)?) {
    self.base = base
    self.onWorkProgress = onWorkProgress
  }

  var maxTokens: Int? { base.maxTokens }
  var tokenCount: Int { base.tokenCount }
  var promptPrefillTime: TimeInterval { base.promptPrefillTime }
  var speculativeDecodingTelemetry: SpeculativeDecodingTelemetry? {
    base.speculativeDecodingTelemetry
  }

  mutating func discardGeneratedToken() { base.discardGeneratedToken() }

  mutating func next() -> Int? {
    guard let token = base.next() else { return nil }
    completedSteps += 1
    if let onWorkProgress {
      // Preparation is unit 1. Capture only this completed count and callback;
      // the operation owner fences late or reordered asynchronous reports.
      let progress = 1 + completedSteps
      Task { await onWorkProgress(progress) }
    }
    return token
  }
}

actor MLXRuntime {
  // iOS ships normal CPU kernels but not CPU JIT. Official compile() is process
  // wide; this app has one MLX owner. Disable it once, never restore unknown
  // global state or change the unsafe global default device while tasks run.
  private static let cpuCompilationDisabled: Void = { MLX.compile(enable: false) }()
  private var container: ModelContainer?
  private var cancelRequested = false
  private var generating = false
  private var generationProducer: Task<Void, Never>?
  private var unloadRequested = false

  func loadLocal(directory: URL) async throws {
    guard !generating else { throw LocalInferenceError.generationInProgress }
    cancelRequested = false
    unloadRequested = false
    #if targetEnvironment(simulator)
    throw LocalInferenceError.unsupportedModel("MLX inference requires a physical iOS device")
    #else
    try await loadContainer(directory: directory)
    #endif
  }

  func loadRemote(modelId: String, revision: String, store: LocalModelStore) async throws {
    guard !generating else { throw LocalInferenceError.generationInProgress }
    cancelRequested = false
    unloadRequested = false
    #if targetEnvironment(simulator)
    throw LocalInferenceError.unsupportedModel("MLX inference requires a physical iOS device")
    #else
    let durable: ResolvedLocalModel
    if let existing = try await store.resolveRemoteMLX(repositoryId: modelId, revision: revision) {
      durable = existing
    } else {
      let components = modelId.split(separator: "/")
      guard components.count == 2 else { throw LocalInferenceError.invalidDownloadMetadata }
      let repository = Repo.ID(namespace: String(components[0]), name: String(components[1]))
      let client = HubClient()
      guard let cache = client.cache else { throw LocalInferenceError.modelDownloadFailed }
      let matching = ["*.json", "*.safetensors", "*.model", "*.txt", "*.jinja", "*.tiktoken"]
      let cachedSnapshot: URL?
      do {
        cachedSnapshot = try await client.downloadSnapshot(
          of: repository, revision: revision, matching: matching, localFilesOnly: true
        )
      } catch HubCacheError.cachedPathResolutionFailed(_) {
        cachedSnapshot = nil
      }
      var preserved: ResolvedLocalModel?
      if let cachedSnapshot {
        do {
          preserved = try await store.preserveMLXSnapshot(
            at: cachedSnapshot, repositoryCacheURL: cache.repoDirectory(repo: repository, kind: .model),
            repositoryId: modelId, revision: revision
          )
        } catch LocalInferenceError.sourceMissing {
          // A partial cache still reuses its existing blobs when missing sidecars/shards are fetched.
          preserved = nil
        }
      }
      if let preserved {
        durable = preserved
      } else {
        try Task.checkCancellation()
        let downloaded = try await client.downloadSnapshot(of: repository, revision: revision, matching: matching)
        durable = try await store.preserveMLXSnapshot(
          at: downloaded, repositoryCacheURL: cache.repoDirectory(repo: repository, kind: .model),
          repositoryId: modelId, revision: revision
        )
      }
    }
    guard !cancelRequested, !Task.isCancelled else { throw CancellationError() }
    try await loadContainer(directory: durable.runtimeURL)
    #endif
  }

  private func loadContainer(directory: URL) async throws {
    let config = try JSONSerialization.jsonObject(with: Data(contentsOf: directory.appendingPathComponent("config.json"))) as? [String: Any]
    guard let config else { throw LocalInferenceError.unsupportedModel("The MLX config must be a JSON object") }
    try LocalModelPurpose.requireGeneration(configuration: config)
    let device = await BackgroundGenerationController.shared.preferredExecutionDevice()
    if device == .cpu {
      _ = Self.cpuCompilationDisabled
      try await Device.withDefaultDevice(.cpu) { @Sendable in
        try Self.verifyCPUActivations()
        try await self.loadContainerOnCurrentDevice(directory: directory)
      }
    } else {
      try await loadContainerOnCurrentDevice(directory: directory)
    }
  }

  private static func verifyCPUExecutionScope() throws {
    // MLX retains one native worker per new CPU stream for the process lifetime.
    // This runtime serializes model work, so reuse the default CPU stream.
    // A task-local stream takes precedence over the scoped device: reject an
    // inherited override before it can schedule work on another stream/device.
    guard Device.defaultDevice().deviceType == .cpu,
          StreamOrDevice.default.stream == Stream.cpu else {
      throw LocalInferenceError.inferenceFailed("The MLX CPU execution scope is unavailable")
    }
  }

  private static func verifyCPUActivations() throws {
    // Exercise the upstream compiled activation entry points on the actual CPU
    // backend before allocating model weights. Their JIT must remain disabled.
    try verifyCPUExecutionScope()
    try MLX.withError {
      let values: [Float] = [-2, -1, 0, 1, 2]
      let input = MLXArray(values)
      let actualSilu = MLXNN.silu(input)
      let actualGelu = MLXNN.gelu(input)
      eval(actualSilu, actualGelu)
      let siluValues = actualSilu.asArray(Float.self)
      let geluValues = actualGelu.asArray(Float.self)
      for index in values.indices {
        let x = Double(values[index])
        let expectedSilu = x / (1 + exp(-x))
        let expectedGelu = x * (1 + erf(x / sqrt(2))) / 2
        guard abs(Double(siluValues[index]) - expectedSilu) < 0.0001,
              abs(Double(geluValues[index]) - expectedGelu) < 0.0001 else {
          throw LocalInferenceError.inferenceFailed("The MLX CPU activation check failed")
        }
      }
      try verifyCPUQuantizedParameters()
    }
  }

  private static func promoteCPUParameters(in model: Module) throws {
    // Keep only names/sizes, not a second snapshot of the original half arrays.
    // Largest first bounds the overlap to the current parameter, not the model.
    let parameters = model.parameters().flattened().compactMap { name, array in
      array.dtype == .float16 || array.dtype == .bfloat16 ? (name, array.nbytes) : nil
    }.sorted { $0.1 > $1.1 }
    for (name, _) in parameters {
      try Task.checkCancellation()
      try autoreleasepool {
        guard let parameter = model.parameters().flattened().first(where: { $0.0 == name })?.1 else {
          throw LocalInferenceError.inferenceFailed("A CPU model parameter is unavailable")
        }
        let promoted = parameter.asType(.float32)
        eval(promoted)
        _ = try model.update(
          parameters: .unflattened([(name, promoted)]),
          verify: [.noUnusedKeys, .shapeMismatch]
        )
      }
      // The old half buffer is no longer referenced after replacement. Do not
      // retain every retired allocation while promoting the remaining arrays.
      Memory.clearCache()
    }
  }

  private static func verifyCPUQuantizedParameters() throws {
    // Exercise the actual affine 4-bit / group-64 path and partial parameter
    // updates on both supported half types, without allocating model weights.
    let words = (0..<32).map { $0 % 2 == 0 ? UInt32(0x76543210) : UInt32(0xfedcba98) }
    let scales: [Float] = [0.5, 1, 2, 0.25]
    let offsets: [Float] = [-2, -1, 0, 1]
    let bias: [Float] = [0.25, 0.5, 0.75, 1]
    let input = (0..<64).map { Float($0 % 5 - 2) }
    for dtype in [DType.float16, .bfloat16] {
      let layer = QuantizedLinear(
        weight: MLXArray(words).reshaped([4, 8]),
        bias: MLXArray(bias).asType(dtype),
        scales: MLXArray(scales).reshaped([4, 1]).asType(dtype),
        biases: MLXArray(offsets).reshaped([4, 1]).asType(dtype),
        groupSize: 64, bits: 4
      )
      try promoteCPUParameters(in: layer)
      let result = layer(MLXArray(input).reshaped([1, 64]).asType(dtype))
      eval(result)
      guard layer.weight.dtype == .uint32, layer.weight.asArray(UInt32.self) == words,
            layer.scales.dtype == .float32, layer.biases?.dtype == .float32,
            layer.bias?.dtype == .float32, result.dtype == .float32 else {
        throw LocalInferenceError.inferenceFailed("The MLX CPU parameter check failed")
      }
      let actual = result.asArray(Float.self)
      for row in 0..<4 {
        var expected = bias[row]
        for column in 0..<64 {
          expected += input[column] * (scales[row] * Float(column % 16) + offsets[row])
        }
        guard actual[row].isFinite, abs(actual[row] - expected) < 0.0001 else {
          throw LocalInferenceError.inferenceFailed("The MLX CPU quantized matrix check failed")
        }
      }
    }
  }

  private func loadContainerOnCurrentDevice(directory: URL) async throws {
    // The upstream iOS recommendation bounds reusable buffers independently of
    // active model weights. The default cache can otherwise grow to several GB.
    Memory.cacheLimit = 20 * 1024 * 1024
    container = nil
    Memory.clearCache()
    defer {
      Memory.clearCache()
      Self.traceMemory("load_exit")
    }

    guard !cancelRequested, !Task.isCancelled else { throw CancellationError() }
    Self.traceMemory("before_tokenizer")
    let tokenizer = try await #huggingFaceTokenizerLoader().load(from: directory)
    guard !cancelRequested, !Task.isCancelled else { throw CancellationError() }
    Self.traceMemory("after_tokenizer")
    let tokenizerLoader = PreloadedMLXTokenizerLoader(directory: directory, tokenizer: tokenizer)
    Memory.clearCache()

    // Model loading evaluates weights through MLX's non-throwing eval API.
    // Convert recoverable MLX errors to throws before publishing a ready model.
    // An OS memory termination cannot be caught here.
    Self.traceMemory("before_weights")
    let loaded = try await MLX.withError {
      try await LLMModelFactory.shared.loadContainer(
        from: directory,
        using: tokenizerLoader
      )
    }
    Self.traceMemory("after_weights")
    guard !cancelRequested, !Task.isCancelled else { throw CancellationError() }
    if Device.defaultDevice().deviceType == .cpu {
      try await loaded.perform { context in
        try MLX.withError {
          try Self.promoteCPUParameters(in: context.model)
        }
      }
      Self.traceMemory("after_cpu_parameters")
    }
    guard !cancelRequested, !Task.isCancelled else { throw CancellationError() }
    container = loaded
  }

  private static func traceMemory(_ stage: String) {
    #if DEBUG
    let snapshot = Memory.snapshot()
    NSLog(
      "MLXMemory stage=%@ active=%llu cache=%llu peak=%llu available=%llu",
      stage, UInt64(snapshot.activeMemory), UInt64(snapshot.cacheMemory),
      UInt64(snapshot.peakMemory), UInt64(os_proc_available_memory())
    )
    #endif
  }

  func generate(
    prompt: String,
    maxTokens: Int,
    temperature: Double,
    executionDevice: BackgroundGenerationDevice = .gpu,
    onOutputProgress: (@Sendable (Int) async -> Void)? = nil,
    onWorkProgress: (@Sendable (Int) async -> Void)? = nil
  ) async throws -> RuntimeGenerationResult {
    if executionDevice == .cpu {
      _ = Self.cpuCompilationDisabled
      return try await Device.withDefaultDevice(.cpu) { @Sendable in
        try Self.verifyCPUExecutionScope()
        return try await self.generateOnCurrentDevice(
          prompt: prompt, maxTokens: maxTokens, temperature: temperature,
          onOutputProgress: onOutputProgress, onWorkProgress: onWorkProgress
        )
      }
    }
    return try await generateOnCurrentDevice(
      prompt: prompt, maxTokens: maxTokens, temperature: temperature,
      onOutputProgress: onOutputProgress, onWorkProgress: onWorkProgress
    )
  }

  private func generateOnCurrentDevice(
    prompt: String, maxTokens: Int, temperature: Double,
    onOutputProgress: (@Sendable (Int) async -> Void)?,
    onWorkProgress: (@Sendable (Int) async -> Void)?
  ) async throws -> RuntimeGenerationResult {
    guard !generating else { throw LocalInferenceError.generationInProgress }
    guard let container else { throw LocalInferenceError.modelNotLoaded }
    let executionStream = StreamOrDevice.default.stream
    generating = true
    cancelRequested = false
    defer {
      // Drain this generation's actual task-local stream even if preparation or
      // TokenIterator construction throws after enqueueing asynchronous work.
      // Any created producer is explicitly joined before reaching this defer.
      executionStream.synchronize()
      generationProducer = nil
      generating = false
      cancelRequested = false
      if unloadRequested {
        self.container = nil
        unloadRequested = false
      }
      Memory.clearCache()
    }

    let promptTokenCount = await container.encode(prompt).count
    guard promptTokenCount + maxTokens <= 4_096 else {
      throw LocalInferenceError.contextExceeded
    }
    if cancelRequested || Task.isCancelled {
      return RuntimeGenerationResult(text: "", finishReason: "cancelled", tokenCount: 0)
    }

    let parameters = GenerateParameters(
      maxTokens: maxTokens,
      maxKVSize: 4_096,
      temperature: Float(temperature),
      topP: 1,
      topK: 0,
      seed: 0
    )
    let events: AsyncStream<Generation>
    let producer: Task<Void, Never>
    do {
      // Match the one-shot ChatSession's user message and processor defaults,
      // but retain the public generation task so cancellation can join it.
      (events, producer) = try await container.perform { context in
        try Task.checkCancellation()
        let input = try await context.processor.prepare(input: UserInput(chat: [.user(prompt)]))
        try Task.checkCancellation()
        await onWorkProgress?(1)
        try Task.checkCancellation()
        let iterator = try TokenIterator(
          input: input, model: context.model, parameters: parameters
        )
        try Task.checkCancellation()
        return MLXLMCommon.generateTask(
          promptTokenCount: input.text.tokens.size,
          modelConfiguration: context.configuration,
          tokenizer: context.tokenizer,
          iterator: WorkReportingTokenIterator(base: iterator, onWorkProgress: onWorkProgress)
        )
      }
    } catch is CancellationError {
      return RuntimeGenerationResult(text: "", finishReason: "cancelled", tokenCount: 0)
    }
    generationProducer = producer
    if cancelRequested || Task.isCancelled { producer.cancel() }

    var output = ""
    var outputBytes = 0
    var completionCount: Int?
    var finishReason = "stop"
    var generationError: (any Error)?
    await withTaskCancellationHandler {
      do {
        try Task.checkCancellation()
        for await event in events {
          if cancelRequested || Task.isCancelled {
            finishReason = "cancelled"
            break
          }
          switch event {
          case .chunk(let value):
            output += value
            outputBytes += value.utf8.count
            await onOutputProgress?(outputBytes)
          case .info(let info):
            completionCount = info.generationTokenCount
            switch info.stopReason {
            case .stop: finishReason = "stop"
            case .length: finishReason = "length"
            case .cancelled: finishReason = "cancelled"
            }
          case .toolCall:
            throw LocalInferenceError.inferenceFailed("unexpected native MLX tool-call output")
          }
        }
      } catch is CancellationError {
        finishReason = "cancelled"
      } catch {
        generationError = error
      }
      // Every exit after task creation joins the exact producer. Stream
      // termination and a cache-lock read alone do not prove task completion.
      producer.cancel()
      await producer.value
    } onCancel: {
      producer.cancel()
    }
    generationProducer = nil
    if cancelRequested || Task.isCancelled { finishReason = "cancelled" }
    if let generationError { throw generationError }

    let tokenCount: Int
    if let completionCount {
      tokenCount = completionCount
    } else {
      tokenCount = await container.encode(output).count
    }
    return RuntimeGenerationResult(text: output, finishReason: finishReason, tokenCount: tokenCount)
  }

  func cancel() {
    cancelRequested = true
    generationProducer?.cancel()
  }

  func unload() {
    cancelRequested = true
    generationProducer?.cancel()
    guard !generating else {
      unloadRequested = true
      return
    }
    container = nil
    unloadRequested = false
    Memory.clearCache()
  }
}
