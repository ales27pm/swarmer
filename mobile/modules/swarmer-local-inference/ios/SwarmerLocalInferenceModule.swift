import ExpoModulesCore
import UIKit

public final class SwarmerLocalInferenceModule: Module {
  private let coordinator = LocalInferenceCoordinator()
  private var directoryPicker: LocalModelDirectoryPicker?
  private var backgroundResignObserver: NSObjectProtocol?
  #if DEBUG
  private let automationServer = AutomationServer(access: { AutomationModuleAccess.current() })
  private let automationEvents = AutomationModuleEventEmitter()
  private var automationLifecycleObservers: [NSObjectProtocol] = []
  #endif

  public func definition() -> ModuleDefinition {
    Name("SwarmerLocalInference")
    Events("automationRequest")

    OnCreate { [weak self, coordinator] in
      self?.backgroundResignObserver = NotificationCenter.default.addObserver(
        forName: UIApplication.willResignActiveNotification, object: nil, queue: .main
      ) { _ in
        // Invalidate pending admission before UIKit's later background event.
        MainActor.assumeIsolated { BackgroundGenerationController.shared.willResignActive() }
        Task { await coordinator.prepareForInactivity() }
      }
    }

    #if DEBUG
    Constants(["automationAvailable": true])

    OnCreate { [weak self] in
      guard let self else { return }
      self.automationEvents.attach(self)
      let server = self.automationServer
      self.automationLifecycleObservers = [
        UIApplication.willResignActiveNotification,
        UIApplication.didEnterBackgroundNotification,
        UIApplication.didBecomeActiveNotification,
        BackgroundGenerationController.statusDidChange,
      ].map { name in
        NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { _ in
          Task { _ = await server.reconcile() }
        }
      }
    }

    AsyncFunction("startAutomationServer") { [automationServer, automationEvents] () async -> AutomationStartRecord in
      let result = await automationServer.start(
        environment: ProcessInfo.processInfo.environment,
        emit: { automationEvents.send($0, access: $1) },
        onReadyChanged: { ready in
          // Server actor transitions enqueue FIFO; UIKit is touched only on main.
          DispatchQueue.main.async { AutomationIdleTimerPolicy.shared.setReady(ready) }
        }
      )
      return AutomationStartRecord(enabled: result.enabled, port: result.port, reason: result.reason)
    }

    AsyncFunction("reconcileAutomationServer") { [automationServer] () async -> AutomationStartRecord in
      let result = await automationServer.reconcile()
      return AutomationStartRecord(enabled: result.enabled, port: result.port, reason: result.reason)
    }

    AsyncFunction("completeAutomationRequest") { [automationServer] (requestId: String, statusCode: Int, bodyJSON: String) async throws -> Void in
      try await automationServer.complete(requestID: requestId, status: statusCode, body: bodyJSON)
    }

    AsyncFunction("stopAutomationServer") { [automationServer] () async -> Void in
      await automationServer.stop()
    }
    #else
    Constants(["automationAvailable": false])

    AsyncFunction("startAutomationServer") { () -> AutomationStartRecord in
      AutomationStartRecord(enabled: false, reason: "debug_build_required")
    }
    AsyncFunction("completeAutomationRequest") { (_: String, _: Int, _: String) -> Void in }
    AsyncFunction("stopAutomationServer") { () -> Void in }
    AsyncFunction("reconcileAutomationServer") { () -> AutomationStartRecord in
      AutomationStartRecord(enabled: false, reason: "debug_build_required")
    }
    #endif

    AsyncFunction("capabilities") { [coordinator] () async -> CapabilitiesRecord in
      await coordinator.capabilities()
    }

    AsyncFunction("importModel") { [coordinator] (options: ImportModelOptions) async throws -> LocalModelRecord in
      try await coordinator.importModel(options: options)
    }

    AsyncFunction("downloadAndImportModel") { [coordinator] (options: DownloadModelOptions) async throws -> LocalModelRecord in
      try await coordinator.downloadAndImportModel(options: options)
    }

    AsyncFunction("cancelModelDownload") { [coordinator] () async -> Void in
      await coordinator.cancelModelDownload()
    }

    AsyncFunction("pickAndImportDirectory") { [weak self, coordinator] (runtime: String, promise: Promise) in
      guard runtime == LocalRuntime.coreML.rawValue || runtime == LocalRuntime.mlx.rawValue else {
        promise.reject("ERR_LOCAL_MODEL_RUNTIME", "Only Core ML and MLX use directory import.")
        return
      }
      guard let self else {
        promise.reject("ERR_LOCAL_MODEL_MODULE", "The local inference module is unavailable.")
        return
      }
      guard self.directoryPicker?.isActive != true else {
        promise.reject("ERR_LOCAL_MODEL_PICKER_BUSY", "A model directory picker is already open.")
        return
      }
      guard let viewController = self.appContext?.utilities?.currentViewController() else {
        promise.reject("ERR_LOCAL_MODEL_PICKER_UI", "No view controller can present the model directory picker.")
        return
      }

      let picker = LocalModelDirectoryPicker { [coordinator] result in
        switch result {
        case .cancelled:
          promise.reject("ERR_LOCAL_MODEL_PICK_CANCELLED", "Model folder selection was cancelled.")
        case .selected(let url):
          let scoped = url.startAccessingSecurityScopedResource()
          guard scoped || FileManager.default.isReadableFile(atPath: url.path) else {
            promise.reject("ERR_LOCAL_MODEL_PICK_ACCESS", "The selected model folder is not readable.")
            return
          }
          Task.detached(priority: .utility) {
            defer {
              if scoped { url.stopAccessingSecurityScopedResource() }
            }
            do {
              let record = try await coordinator.importModel(
                runtimeValue: runtime,
                uri: url.absoluteString,
                displayName: url.lastPathComponent
              )
              promise.resolve(record)
            } catch {
              promise.reject(error)
            }
          }
        }
      }
      self.directoryPicker = picker
      picker.present(from: viewController)
    }.runOnQueue(.main)

    AsyncFunction("listModels") { [coordinator] () async throws -> [LocalModelRecord] in
      try await coordinator.listModels()
    }

    AsyncFunction("loadModel") { [coordinator] (options: LoadModelOptions) async throws -> StatusRecord in
      try await coordinator.loadModel(options: options)
    }

    AsyncFunction("getBackgroundExecutionStatus") { () async -> BackgroundExecutionRecord in
      let snapshot = await BackgroundGenerationController.shared.status()
      return BackgroundExecutionRecord(snapshot)
    }

    AsyncFunction("embeddingStatus") { [coordinator] () async -> EmbeddingStatusRecord in
      await coordinator.embeddingStatus()
    }
    AsyncFunction("loadEmbedder") { [coordinator] (options: LoadEmbedderOptions) async throws -> EmbeddingStatusRecord in
      try await coordinator.loadEmbedder(options: options)
    }
    AsyncFunction("embed") { [coordinator] (options: EmbedOptions) async throws -> EmbeddingResultRecord in
      try await coordinator.embed(options: options)
    }
    AsyncFunction("unloadEmbedder") { [coordinator] () async -> Void in
      await coordinator.unloadEmbedder()
    }

    AsyncFunction("status") { [coordinator] () async -> StatusRecord in
      await coordinator.status()
    }

    AsyncFunction("generate") { [coordinator] (options: GenerateOptions) async throws -> GenerationRecord in
      try await coordinator.generate(options: options)
    }

    AsyncFunction("cancel") { [coordinator] () async -> Void in
      await coordinator.cancel()
    }

    AsyncFunction("unload") { [coordinator] () async -> Void in
      await coordinator.unload()
    }

    OnAppEntersBackground { [coordinator] in
      Task { await coordinator.suspend() }
    }

    #if DEBUG
    OnDestroy { [weak self, automationServer] in
      self?.automationLifecycleObservers.forEach { NotificationCenter.default.removeObserver($0) }
      self?.automationLifecycleObservers.removeAll()
      Task { await automationServer.stop(reason: "destroyed") }
    }
    #endif

    OnAppBecomesActive { [coordinator] in
      Task {
        await BackgroundGenerationController.shared.didBecomeActive()
        await coordinator.resume()
      }
    }

    OnDestroy { [weak self, coordinator] in
      if let observer = self?.backgroundResignObserver {
        NotificationCenter.default.removeObserver(observer)
        self?.backgroundResignObserver = nil
      }
      Task { await coordinator.shutdown() }
    }
  }
}

struct BackgroundExecutionRecord: Record, Sendable {
  @Field var supported: Bool = false
  @Field var reason: String? = nil
  @Field var osSupported: Bool = false
  @Field var gpuSupported: Bool = false
  @Field var entitlementGranted: Bool? = nil
  @Field var active: Bool = false
  @Field var operationId: String? = nil
  @Field var outputBytes: Int = 0
  @Field var state: String = "idle"
  @Field var executionDevice: String? = nil

  init() {}
  init(_ snapshot: BackgroundExecutionSnapshot) {
    self.init()
    supported = snapshot.supported
    reason = snapshot.reason
    osSupported = snapshot.osSupported
    gpuSupported = snapshot.gpuSupported
    entitlementGranted = snapshot.entitlementGranted
    active = snapshot.active
    operationId = snapshot.operationId
    outputBytes = snapshot.outputBytes
    state = snapshot.state
    executionDevice = snapshot.executionDevice
  }
}
