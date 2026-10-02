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
    let diagnostic: CoreMLLoadDiagnostic?
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
    let isDownload: Bool
    let task: Task<StoredLocalModel, Error>
  }

  private let store = LocalModelStore()
  private let embedder = MLXEmbeddingRuntime()
  private var embeddingReserved = false
  private var diagnosticReserved = false
  #if DEBUG
  private struct FixtureProbeOperation: Sendable {
    let id: UUID
    let task: Task<CoreMLFixtureProbe.Report, Never>
  }
  private var fixtureProbeOperation: FixtureProbeOperation?
  private var diagnosticImportID: UUID?
  #endif
  private var embeddingEpoch = 0
  private var handle: Handle?
  private var loadOperation: LoadOperation?
  private var generationOperation: GenerationOperation?
  private var importOperation: ImportOperation?
  private var currentRuntime: LocalRuntime?
  private var currentModelId: String?
  private var currentRevision: String?
  private var state = "idle"
  private var message: String?
  private var coreMLLoadDiagnostic: CoreMLLoadDiagnostic?
  private var lifecycleEpoch = 0
  private var activity = GenerationActivityFence()

  func embeddingStatus() async -> EmbeddingStatusRecord { await embedder.status() }

  func loadEmbedder(options: LoadEmbedderOptions) async throws -> EmbeddingStatusRecord {
    let snapshot = await BackgroundGenerationController.shared.activitySnapshot()
    guard !snapshot.inactive else { throw LocalInferenceError.generationInProgress }
    guard !diagnosticReserved, !embeddingReserved, !activity.isSuspended, handle == nil,
          state == "idle" || state == "failed",
          loadOperation == nil, generationOperation == nil, importOperation == nil else {
      throw LocalInferenceError.generationInProgress
    }
    embeddingEpoch += 1
    let epoch = embeddingEpoch
    embeddingReserved = true
    do { return try await embedder.load(options: options, store: store) }
    catch { if embeddingEpoch == epoch { embeddingReserved = false }; throw error }
  }

  func embed(options: EmbedOptions) async throws -> EmbeddingResultRecord {
    let snapshot = await BackgroundGenerationController.shared.activitySnapshot()
    guard !snapshot.inactive else { throw LocalInferenceError.generationInProgress }
    guard !diagnosticReserved, embeddingReserved, !activity.isSuspended else { throw LocalInferenceError.modelNotLoaded }
    return try await embedder.embed(options: options)
  }

  func unloadEmbedder() async {
    embeddingEpoch += 1
    let epoch = embeddingEpoch
    embeddingReserved = true
    await embedder.unload()
    if embeddingEpoch == epoch { embeddingReserved = false }
  }

  func capabilities() -> CapabilitiesRecord {
    CapabilitiesRecord()
  }

  #if DEBUG
  func importCoreMLDiagnosticCandidate() async throws -> LocalModelRecord {
    let manifest = try CoreMLDiagnosticCandidate.bundledManifest()
    let snapshot = await BackgroundGenerationController.shared.activitySnapshot()
    guard !snapshot.inactive, !activity.isSuspended, !diagnosticReserved, !embeddingReserved,
          handle == nil, state == "idle" || state == "failed", importOperation == nil,
          loadOperation == nil, generationOperation == nil else { throw LocalInferenceError.generationInProgress }
    let id = UUID()
    diagnosticReserved = true
    diagnosticImportID = id
    defer {
      if diagnosticImportID == id {
        diagnosticImportID = nil
        diagnosticReserved = false
      }
    }
    let store = self.store
    let task = Task.detached(priority: .utility) {
      try await store.importCoreMLDiagnosticCandidate(manifest: manifest)
    }
    return try await withTaskCancellationHandler {
      try await finishImport(task, operationId: id, isDownload: false)
    } onCancel: {
      task.cancel()
    }
  }

  func probeCoreMLFixture(fixtureID: String, computeUnits: String) async throws -> String {
    guard CoreMLFixtureProbe.fixtureIDs.contains(fixtureID),
          let units = try CoreMLDiagnosticComputeUnits.requested(computeUnits, runtime: "coreml") else {
      throw CoreMLDiagnosticOptionError()
    }
    let snapshot = await BackgroundGenerationController.shared.activitySnapshot()
    guard !snapshot.inactive, !activity.isSuspended, !diagnosticReserved, !embeddingReserved,
          handle == nil, state == "idle" || state == "failed", importOperation == nil,
          loadOperation == nil, generationOperation == nil else { throw LocalInferenceError.generationInProgress }
    let id = UUID()
    diagnosticReserved = true
    state = "loading"
    message = nil
    currentRuntime = nil
    currentModelId = nil
    currentRevision = nil
    coreMLLoadDiagnostic = nil
    let task = Task.detached(priority: .userInitiated) {
      await CoreMLFixtureProbe.run(fixtureID: fixtureID, units: units)
    }
    fixtureProbeOperation = FixtureProbeOperation(id: id, task: task)
    let watchdog = Task {
      do {
        try await Task.sleep(nanoseconds: CoreMLFixtureProbe.timeoutSeconds * 1_000_000_000)
        await self.cancelFixtureProbe(id: id)
      } catch { /* The completed probe cancels its watchdog. */ }
    }
    defer { watchdog.cancel() }
    var report = await withTaskCancellationHandler {
      await task.value
    } onCancel: {
      task.cancel()
    }
    // A cancellation racing with completion must never publish success. Keep
    // exclusive admission until the Core ML task has actually returned.
    if task.isCancelled || Task.isCancelled {
      report.outcome = "cancelled"
      report.errors = [CoreMLLoadDiagnostic.Failure(
        domain: NSCocoaErrorDomain, code: NSUserCancelledError, executionPlanCode: nil
      )]
    }
    if fixtureProbeOperation?.id == id {
      fixtureProbeOperation = nil
      diagnosticReserved = false
      state = "idle"
      message = nil
    }
    return try report.json()
  }

  private func cancelFixtureProbe(id: UUID) async {
    guard let operation = fixtureProbeOperation, operation.id == id else { return }
    state = "cancelling"
    operation.task.cancel()
    _ = await operation.task.value
    if fixtureProbeOperation?.id == id {
      fixtureProbeOperation = nil
      diagnosticReserved = false
      state = "idle"
      message = nil
    }
  }
  #endif

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
    guard !diagnosticReserved, !embeddingReserved, !activity.isSuspended,
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
    return try await finishImport(task, operationId: operationId, isDownload: false)
  }

  func downloadAndImportModel(options: DownloadModelOptions) async throws -> LocalModelRecord {
    let download = try LocalModelDownload(
      repoId: options.repoId,
      revision: options.revision,
      filename: options.filename,
      sha256: options.sha256,
      sizeBytes: options.sizeBytes,
      displayName: options.displayName
    )
    guard !diagnosticReserved, !embeddingReserved, !activity.isSuspended,
          importOperation == nil,
          loadOperation == nil,
          generationOperation == nil,
          state != "cancelling" else {
      throw LocalInferenceError.generationInProgress
    }
    let operationId = UUID()
    let task = Task.detached(priority: .utility) { [store] in
      try await download.downloadAndImport(into: store)
    }
    return try await finishImport(task, operationId: operationId, isDownload: true)
  }

  func cancelModelDownload() async {
    guard importOperation?.isDownload == true else { return }
    await cancelImport()
  }

  private func finishImport(
    _ task: Task<StoredLocalModel, Error>,
    operationId: UUID,
    isDownload: Bool
  ) async throws -> LocalModelRecord {
    importOperation = ImportOperation(id: operationId, isDownload: isDownload, task: task)
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
    var models: [LocalModelRecord] = []
    for stored in try await store.list() {
      models.append(LocalModelRecord(stored: stored, purpose: try await store.purpose(for: stored)))
    }
    return models
  }

  func loadModel(options: LoadModelOptions) async throws -> StatusRecord {
    guard !diagnosticReserved, !embeddingReserved, !activity.isSuspended,
          importOperation == nil,
          state != "loading", state != "generating", state != "cancelling" else {
      throw LocalInferenceError.generationInProgress
    }
    let requestedRuntime = try LocalRuntime(wireValue: options.runtime)
    // Reject diagnostics in Release, and for other engines, before unloading a model.
    let diagnosticUnits = try CoreMLDiagnosticComputeUnits.requested(
      options.coreMLComputeUnits, runtime: requestedRuntime.rawValue
    )
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
    coreMLLoadDiagnostic = nil

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
        revision: options.revision,
        diagnosticUnits: diagnosticUnits
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
      coreMLLoadDiagnostic = outcome.diagnostic
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
        coreMLLoadDiagnostic = (error as? CoreMLDiagnosticLoadFailure)?.diagnostic
      }
      if (error as? CoreMLDiagnosticLoadFailure)?.diagnostic.outcome == .cancelled {
        throw CancellationError()
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
      message: message,
      coreMLLoadDiagnostic: coreMLLoadDiagnostic
    )
  }

  func generate(options: GenerateOptions) async throws -> GenerationRecord {
    guard let handle else { throw LocalInferenceError.modelNotLoaded }
    guard !diagnosticReserved, !activity.isSuspended, importOperation == nil, state == "ready", generationOperation == nil else {
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
        try Task.checkCancellation()
        let executionDevice = await BackgroundGenerationController.shared.prepare(operationId: operationId, maxTokens: maxTokens) {
          await self.cancel(operationId: operationId)
        }
        guard await self.mayBeginGeneration(operationId: operationId) else { throw CancellationError() }
        try Task.checkCancellation()
        return try await runtime.generate(
          prompt: prompt,
          maxTokens: maxTokens,
          temperature: temperature,
          executionDevice: executionDevice,
          onOutputProgress: { bytes in
            await BackgroundGenerationController.shared.reportOutput(operationId: operationId, bytes: bytes)
          },
          onWorkProgress: { units in
            await BackgroundGenerationController.shared.reportWork(operationId: operationId, completedUnits: units)
          }
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
      let wasCancelled = result.finishReason == "cancelled" || task.isCancelled
      let leaseCancelled = await BackgroundGenerationController.shared.finish(
        operationId: operationId, success: !wasCancelled, cancelled: wasCancelled
      )
      // Cancellation can arrive after MLX's .info event but before synchronize
      // or while this actor awaits lease completion. Never publish a valid plan.
      let finishReason = wasCancelled || leaseCancelled || task.isCancelled ? "cancelled" : result.finishReason
      if generationOperation?.id == operationId {
        generationOperation = nil
        state = self.handle == nil ? "idle" : "ready"
        message = nil
      }
      return GenerationRecord(
        text: result.text,
        finishReason: finishReason,
        tokenCount: result.tokenCount
      )
    } catch is CancellationError {
      await BackgroundGenerationController.shared.finish(operationId: operationId, success: false, cancelled: true)
      if generationOperation?.id == operationId {
        generationOperation = nil
        state = self.handle == nil ? "idle" : "ready"
        message = nil
      }
      return GenerationRecord(text: "", finishReason: "cancelled", tokenCount: 0)
    } catch {
      await BackgroundGenerationController.shared.finish(operationId: operationId, success: false)
      if generationOperation?.id == operationId {
        generationOperation = nil
        state = "failed"
        message = error.localizedDescription
      }
      throw error
    }
  }

  private func mayBeginGeneration(operationId: UUID) async -> Bool {
    let allowed = await BackgroundGenerationController.shared.mayRun(operationId: operationId)
    return allowed && generationOperation?.id == operationId && state == "generating"
  }

  func cancel() async {
    #if DEBUG
    if let operation = fixtureProbeOperation {
      await cancelFixtureProbe(id: operation.id)
      return
    }
    #endif
    guard let operation = generationOperation else { return }
    await cancel(operationId: operation.id)
  }

  private func cancel(operationId: UUID) async {
    guard let operation = generationOperation, operation.id == operationId else { return }
    state = "cancelling"
    operation.task.cancel()
    await Self.cancel(operation.handle)
    _ = await operation.task.result
    await BackgroundGenerationController.shared.finish(operationId: operation.id, success: false, cancelled: true)
    if generationOperation?.id == operation.id {
      generationOperation = nil
      state = handle == nil ? "idle" : "ready"
      message = nil
    }
  }

  func unload() async {
    lifecycleEpoch += 1
    let invalidatedEpoch = lifecycleEpoch
    #if DEBUG
    if let operation = fixtureProbeOperation { await cancelFixtureProbe(id: operation.id) }
    #endif
    if importOperation != nil || loadOperation != nil || generationOperation != nil {
      state = "cancelling"
    }

    await cancelImport()

    if let operation = generationOperation {
      operation.task.cancel()
      await Self.cancel(operation.handle)
      _ = await operation.task.result
      await BackgroundGenerationController.shared.finish(operationId: operation.id, success: false, cancelled: true)
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
    coreMLLoadDiagnostic = nil
  }

  func shutdown() async {
    activity.shutdown()
    await unloadEmbedder()
    await unload()
  }

  func prepareForInactivity() async {
    #if DEBUG
    if diagnosticImportID != nil { await cancelImport() }
    if let operation = fixtureProbeOperation { await cancelFixtureProbe(id: operation.id) }
    #endif
    if embeddingReserved { await unloadEmbedder() }
    await reconcileActivity(cancelAllWhenInactive: false)
  }

  func suspend() async { await reconcileActivity(cancelAllWhenInactive: true) }

  func resume() async { await reconcileActivity(cancelAllWhenInactive: false) }

  private func reconcileActivity(cancelAllWhenInactive: Bool) async {
    let epoch = activity.begin()
    let snapshot = await BackgroundGenerationController.shared.activitySnapshot()
    guard activity.reconcile(inactive: snapshot.inactive, epoch: epoch), snapshot.inactive else { return }
    #if DEBUG
    if diagnosticImportID != nil {
      await cancelImport()
      guard activity.isCurrent(epoch), activity.isSuspended else { return }
    }
    if let operation = fixtureProbeOperation {
      await cancelFixtureProbe(id: operation.id)
      guard activity.isCurrent(epoch), activity.isSuspended else { return }
    }
    #endif
    let cancelAll = snapshot.shouldCancelAll(requested: cancelAllWhenInactive)
    if let operation = generationOperation, case .mlx = operation.handle {
      let admitted = await BackgroundGenerationController.shared.mayContinue(operationId: operation.id)
      guard activity.isCurrent(epoch), activity.isSuspended else { return }
      if admitted, generationOperation?.id == operation.id {
        // Only the already running generation with an admitted GPU lease survives.
        return
      }
      if !cancelAll {
        await cancel(operationId: operation.id)
        return
      }
    }
    guard cancelAll else { return }
    await cancelImport()
    guard activity.isCurrent(epoch), activity.isSuspended else { return }
    if loadOperation != nil {
      await unload()
    } else if let operation = generationOperation {
      await cancel(operationId: operation.id)
    } else if state == "cancelling" {
      state = handle == nil ? "idle" : "ready"
      message = nil
    }
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
    revision: String?,
    diagnosticUnits: CoreMLDiagnosticComputeUnits?
  ) async throws -> LoadOutcome {
    var loading: Handle?
    let startedAt = ProcessInfo.processInfo.systemUptime
    var diagnosticStage = CoreMLLoadDiagnostic.Stage.resolve
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
      var diagnostic: CoreMLLoadDiagnostic?
      if let resolved {
        guard resolved.stored.runtime == runtime else {
          throw LocalInferenceError.runtimeMismatch
        }
        guard try await store.purpose(for: resolved.stored) == .generation else {
          throw LocalInferenceError.unsupportedModel("This model provides embeddings. Use the semantic-memory embeddings controls.")
        }
        switch runtime {
        case .coreML:
          guard let tokenizerURL = resolved.tokenizerURL else {
            throw LocalInferenceError.unsupportedModel("the Core ML tokenizer sidecar is missing")
          }
          let runtime = CoreMLRuntime()
          loaded = .coreML(runtime)
          loading = loaded
          diagnostic = try await runtime.load(
            modelURL: resolved.runtimeURL, tokenizerURL: tokenizerURL, diagnosticUnits: diagnosticUnits
          )
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
        guard modelId != EmbeddingValidation.repository else {
          throw LocalInferenceError.unsupportedModel("E5 provides embeddings, not language generation. Use the semantic-memory embeddings controls.")
        }
        let immutableRevision = try LocalInferenceValidation.immutableRevision(revision)
        resolvedRevision = immutableRevision
        let runtime = MLXRuntime()
        loaded = .mlx(runtime)
        loading = loaded
        try await runtime.loadRemote(modelId: modelId, revision: immutableRevision, store: store)
      }

      diagnosticStage = .complete
      try Task.checkCancellation()
      return LoadOutcome(handle: loaded, revision: resolvedRevision, diagnostic: diagnostic)
    } catch {
      if let loading {
        await Self.cancel(loading)
        await Self.unload(loading)
      }
      if let units = diagnosticUnits, !(error is CoreMLDiagnosticLoadFailure) {
        throw CoreMLDiagnosticLoadFailure(diagnostic: .capture(
          units: units, stage: diagnosticStage, startedAt: startedAt, error: error
        ))
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
