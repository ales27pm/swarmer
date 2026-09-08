import ExpoModulesCore

public final class SwarmerLocalInferenceModule: Module {
  private let coordinator = LocalInferenceCoordinator()
  private var directoryPicker: LocalModelDirectoryPicker?

  public func definition() -> ModuleDefinition {
    Name("SwarmerLocalInference")

    AsyncFunction("capabilities") { [coordinator] () async -> CapabilitiesRecord in
      await coordinator.capabilities()
    }

    AsyncFunction("importModel") { [coordinator] (options: ImportModelOptions) async throws -> LocalModelRecord in
      try await coordinator.importModel(options: options)
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

    OnAppBecomesActive { [coordinator] in
      Task { await coordinator.resume() }
    }

    OnDestroy { [coordinator] in
      Task { await coordinator.shutdown() }
    }
  }
}
