import Foundation

actor LocalInferenceCoordinator {
  private enum Handle: Sendable {
    case coreML(CoreMLRuntime)
    case mlx(MLXRuntime)
    case llamaCpp(LlamaCppRuntime)
  }

  private struct LoadOutcome: Sendable {
    let handle: Handle
    let revision: String?
  }

  private struct LoadOperation: Sendable {
    let id: UUID
    let epoch: Int
    let task: Task<LoadOutcome, Error>
  }

  private struct GenerationOperation: Sendable {
    let id: UUID
    let handle: Handle
    let task: Task<RuntimeGenerationResult, Error>
  }

  private struct ImportOperation: Sendable {
    let id: UUID
    let task: Task<StoredLocalModel, Error>
  }

  private let store = LocalModelStore()
  private var handle: Handle?
  private var loadOperation: LoadOperation?
  private var generationOperation: GenerationOperation?
  private var importOperation: ImportOperation?
  private var currentRuntime: LocalRuntime?
  private var currentModelId: String?
  private var currentRevision: String?
  private var state = "idle"
  private var message: String?
  private var lifecycleEpoch = 0
  private var isSuspended = false

  func capabilities() -> CapabilitiesRecord {
    CapabilitiesRecord()
  }

  func importModel(options: ImportModelOptions) async throws -> LocalModelRecord {
    try await importModel(
      runtimeValue: options.runtime,
      uri: options.uri,
      displayName: options.displayName
    )
  }

  func importModel(
    runtimeValue: String,
    uri: String,
    displayName: String?
  ) async throws -> LocalModelRecord {
    let runtime = try LocalRuntime(wireValue: runtimeValue)
    guard !isSuspended,
          importOperation == nil,
          loadOperation == nil,
          generationOperation == nil,
          state != "cancelling" else {
      throw LocalInferenceError.generationInProgress
    }
    let operationId = UUID()
    let task = Task.detached(priority: .utility) { [store] in
      try await store.importModel(
        runtime: runtime,
        uri: uri,
        displayName: displayName
      )
    }
    importOperation = ImportOperation(id: operationId, task: task)
    do {
      let imported = try await task.value
      if importOperation?.id == operationId {
        importOperation = nil
      }
      return LocalModelRecord(stored: imported)
    } catch {
      if importOperation?.id == operationId {
        importOperation = nil
      }
      throw error
    }
  }

  func listModels() async throws -> [LocalModelRecord] {
    try await store.list().map(LocalModelRecord.init(stored:))
  }

  func loadModel(options: LoadModelOptions) async throws -> StatusRecord {
    guard !isSuspended,
          importOperation == nil,
          state != "loading", state != "generating", state != "cancelling" else {
      throw LocalInferenceError.generationInProgress
    }
    let requestedRuntime = try LocalRuntime(wireValue: options.runtime)
    let requestedId = options.modelId.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !requestedId.isEmpty, requestedId.count <= 200 else {
      throw LocalInferenceError.modelNotFound(options.modelId)
    }

    lifecycleEpoch += 1
    let loadEpoch = lifecycleEpoch
    state = "loading"
    currentRuntime = requestedRuntime
    currentModelId = requestedId
    currentRevision = nil
    message = nil

    if let previous = handle {
      handle = nil
      await Self.cancel(previous)
      await Self.unload(previous)
    }
    guard lifecycleEpoch == loadEpoch else { throw CancellationError() }

    let operationId = UUID()
    let task = Task.detached(priority: .userInitiated) { [store] in
      try await Self.performLoad(
        store: store,
        runtime: requestedRuntime,
        modelId: requestedId,
        revision: options.revision
      )
    }
    loadOperation = LoadOperation(id: operationId, epoch: loadEpoch, task: task)

    do {
      let outcome = try await task.value
      guard lifecycleEpoch == loadEpoch,
            loadOperation?.id == operationId,
            loadOperation?.epoch == loadEpoch else {
        // unload() owns cleanup after it invalidates this epoch.
        throw CancellationError()
      }
      loadOperation = nil
      handle = outcome.handle
      currentRevision = outcome.revision
      state = "ready"
      message = nil
      return status()
    } catch {
      if loadOperation?.id == operationId {
        loadOperation = nil
      }
      if lifecycleEpoch == loadEpoch {
        handle = nil
        state = "failed"
        message = error.localizedDescription
      }
      throw error
    }
  }

  func status() -> StatusRecord {
    StatusRecord(
      state: state,
      runtime: currentRuntime,
      modelId: currentModelId,
      revision: currentRevision,
      message: message
    )
  }

  func generate(options: GenerateOptions) async throws -> GenerationRecord {
    guard let handle else { throw LocalInferenceError.modelNotLoaded }
    guard !isSuspended, importOperation == nil, state == "ready", generationOperation == nil else {
      throw LocalInferenceError.generationInProgress
    }
    let prompt = try LocalInferenceValidation.prompt(options.prompt)
    let maxTokens = try LocalInferenceValidation.maxTokens(options.maxTokens)
    let temperature = try LocalInferenceValidation.temperature(options.temperature)
    let operationId = UUID()
    state = "generating"
    message = nil

    let task = Task.detached(priority: .userInitiated) {
      switch handle {
      case .coreML(let runtime):
        return try await runtime.generate(
          prompt: prompt,
          maxTokens: maxTokens,
          temperature: temperature
        )
      case .mlx(let runtime):
        return try await runtime.generate(
          prompt: prompt,
          maxTokens: maxTokens,
          temperature: temperature
        )
      case .llamaCpp(let runtime):
        return try await runtime.generate(
          prompt: prompt,
          maxTokens: maxTokens,
          temperature: temperature
        )
      }
    }
    generationOperation = GenerationOperation(id: operationId, handle: handle, task: task)

    do {
      let result = try await task.value
      if generationOperation?.id == operationId {
        generationOperation = nil
        state = self.handle == nil ? "idle" : "ready"
        message = nil
      }
      return GenerationRecord(
        text: result.text,
        finishReason: result.finishReason,
        tokenCount: result.tokenCount
      )
    } catch is CancellationError {
      if generationOperation?.id == operationId {
        generationOperation = nil
        state = self.handle == nil ? "idle" : "ready"
        message = nil
      }
      return GenerationRecord(text: "", finishReason: "cancelled", tokenCount: 0)
    } catch {
      if generationOperation?.id == operationId {
        generationOperation = nil
        state = "failed"
        message = error.localizedDescription
      }
      throw error
    }
  }

  func cancel() async {
    guard let operation = generationOperation else { return }
    state = "cancelling"
    operation.task.cancel()
    await Self.cancel(operation.handle)
    _ = await operation.task.result
    if generationOperation?.id == operation.id {
      generationOperation = nil
      state = handle == nil ? "idle" : "ready"
      message = nil
    }
  }

  func unload() async {
    lifecycleEpoch += 1
    let invalidatedEpoch = lifecycleEpoch
    if importOperation != nil || loadOperation != nil || generationOperation != nil {
      state = "cancelling"
    }

    await cancelImport()

    if let operation = generationOperation {
      operation.task.cancel()
      await Self.cancel(operation.handle)
      _ = await operation.task.result
      if generationOperation?.id == operation.id {
        generationOperation = nil
      }
    }

    if let operation = loadOperation {
      operation.task.cancel()
      let result = await operation.task.result
      if case .success(let outcome) = result {
        await Self.cancel(outcome.handle)
        await Self.unload(outcome.handle)
      }
      if loadOperation?.id == operation.id {
        loadOperation = nil
      }
    }

    if let loaded = handle {
      handle = nil
      await Self.cancel(loaded)
      await Self.unload(loaded)
    }

    // A later lifecycle operation owns state if actor reentrancy advanced again.
    guard lifecycleEpoch == invalidatedEpoch else { return }
    currentRuntime = nil
    currentModelId = nil
    currentRevision = nil
    state = "idle"
    message = nil
  }

  func shutdown() async {
    isSuspended = true
    await unload()
  }

  func suspend() async {
    isSuspended = true
    if importOperation != nil || loadOperation != nil || generationOperation != nil {
      state = "cancelling"
    }
    await cancelImport()
    if loadOperation != nil {
      await unload()
    } else if generationOperation != nil {
      await cancel()
    } else if state == "cancelling" {
      state = handle == nil ? "idle" : "ready"
      message = nil
    }
  }

  func resume() {
    isSuspended = false
  }

  private func cancelImport() async {
    guard let operation = importOperation else { return }
    operation.task.cancel()
    _ = await operation.task.result
    if importOperation?.id == operation.id {
      importOperation = nil
    }
  }

  private static func performLoad(
    store: LocalModelStore,
    runtime: LocalRuntime,
    modelId: String,
    revision: String?
  ) async throws -> LoadOutcome {
    var loading: Handle?
    do {
      let resolved: ResolvedLocalModel?
      do {
        resolved = try await store.resolve(modelId: modelId)
      } catch LocalInferenceError.modelNotFound(_) {
        resolved = nil
      }
      try Task.checkCancellation()

      let loaded: Handle
      var resolvedRevision: String?
      if let resolved {
        guard resolved.stored.runtime == runtime else {
          throw LocalInferenceError.runtimeMismatch
        }
        switch runtime {
        case .coreML:
          guard let tokenizerURL = resolved.tokenizerURL else {
            throw LocalInferenceError.unsupportedModel("the Core ML tokenizer sidecar is missing")
          }
          let runtime = CoreMLRuntime()
          loaded = .coreML(runtime)
          loading = loaded
          try await runtime.load(modelURL: resolved.runtimeURL, tokenizerURL: tokenizerURL)
        case .mlx:
          let runtime = MLXRuntime()
          loaded = .mlx(runtime)
          loading = loaded
          try await runtime.loadLocal(directory: resolved.runtimeURL)
        case .llamaCpp:
          let runtime = LlamaCppRuntime()
          loaded = .llamaCpp(runtime)
          loading = loaded
          try await runtime.load(modelURL: resolved.runtimeURL)
        }
      } else {
        guard runtime == .mlx, Self.isHuggingFaceModelId(modelId) else {
          throw LocalInferenceError.modelNotFound(modelId)
        }
        let immutableRevision = try LocalInferenceValidation.immutableRevision(revision)
        resolvedRevision = immutableRevision
        let runtime = MLXRuntime()
        loaded = .mlx(runtime)
        loading = loaded
        try await runtime.loadRemote(modelId: modelId, revision: immutableRevision)
      }

      try Task.checkCancellation()
      return LoadOutcome(handle: loaded, revision: resolvedRevision)
    } catch {
      if let loading {
        await Self.cancel(loading)
        await Self.unload(loading)
      }
      throw error
    }
  }

  private static func unload(_ handle: Handle) async {
    switch handle {
    case .coreML(let runtime): await runtime.unload()
    case .mlx(let runtime): await runtime.unload()
    case .llamaCpp(let runtime): await runtime.unload()
    }
  }

  private static func cancel(_ handle: Handle) async {
    switch handle {
    case .coreML(let runtime): await runtime.cancel()
    case .mlx(let runtime): await runtime.cancel()
    case .llamaCpp(let runtime): await runtime.cancel()
    }
  }

  private static func isHuggingFaceModelId(_ value: String) -> Bool {
    value.range(
      of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$",
      options: .regularExpression
    ) != nil
  }
}
