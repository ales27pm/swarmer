import ExpoModulesCore
import Foundation

enum LocalRuntime: String, Codable, Sendable {
  case coreML = "coreml"
  case mlx
  case llamaCpp = "llama.cpp"

  init(wireValue: String) throws {
    guard let value = Self(rawValue: wireValue) else {
      throw LocalInferenceError.unsupportedRuntime(wireValue)
    }
    self = value
  }
}

struct StoredLocalModel: Codable, Equatable, Sendable {
  let modelId: String
  let runtime: LocalRuntime
  let displayName: String
  let source: String
  let sizeBytes: Int64
  let importedAt: Date
  let runtimeRelativePath: String
  let tokenizerRelativePath: String?
}

struct ResolvedLocalModel: Sendable {
  let stored: StoredLocalModel
  let runtimeURL: URL
  let tokenizerURL: URL?
}

struct RuntimeGenerationResult: Sendable {
  let text: String
  let finishReason: String
  let tokenCount: Int
}

struct ImportModelOptions: Record, Sendable {
  @Field var runtime: String = ""
  @Field var uri: String = ""
  @Field var displayName: String? = nil
}

struct LoadModelOptions: Record, Sendable {
  @Field var runtime: String = ""
  @Field var modelId: String = ""
  @Field var revision: String? = nil
}

struct GenerateOptions: Record, Sendable {
  @Field var prompt: String = ""
  @Field var maxTokens: Int? = nil
  @Field var temperature: Double? = nil
}

struct CapabilitiesRecord: Record, Sendable {
  @Field var coreml: Bool = true
  @Field var mlx: Bool = true
  @Field var llamaCpp: Bool = true
  @Field var platform: String = "ios"
  @Field var reasons: [String: String] = [:]

  init() {
    #if targetEnvironment(simulator)
    mlx = false
    llamaCpp = false
    reasons = [
      "mlx": "MLX model execution is enabled only on a physical Apple-silicon iOS device.",
      "llamaCpp": "The pinned upstream llama.cpp XCFramework does not contain an iOS Simulator slice."
    ]
    #endif
  }
}

struct LocalModelRecord: Record, Sendable {
  @Field var modelId: String = ""
  @Field var runtime: String = ""
  @Field var displayName: String = ""
  @Field var source: String = ""
  @Field var sizeBytes: Int64 = 0
  @Field var importedAt: String = ""

  init() {}

  init(stored: StoredLocalModel) {
    self.init()
    modelId = stored.modelId
    runtime = stored.runtime.rawValue
    displayName = stored.displayName
    source = stored.source
    sizeBytes = stored.sizeBytes
    importedAt = ISO8601DateFormatter().string(from: stored.importedAt)
  }
}

struct StatusRecord: Record, Sendable {
  @Field var state: String = "idle"
  @Field var runtime: String? = nil
  @Field var modelId: String? = nil
  @Field var revision: String? = nil
  @Field var message: String = ""

  init() {}

  init(state: String, runtime: LocalRuntime?, modelId: String?, revision: String?, message: String?) {
    self.init()
    self.state = state
    self.runtime = runtime?.rawValue
    self.modelId = modelId
    self.revision = revision
    self.message = message ?? ""
  }
}

struct GenerationRecord: Record, Sendable {
  @Field var text: String = ""
  @Field var finishReason: String = "stop"
  @Field var tokenCount: Int = 0

  init() {}

  init(result: RuntimeGenerationResult) {
    self.init()
    text = result.text
    finishReason = result.finishReason
    tokenCount = result.tokenCount
  }
}

enum LocalInferenceError: LocalizedError, Sendable {
  case unsupportedRuntime(String)
  case invalidURI
  case sourceMissing
  case importTooLarge
  case importHasTooManyFiles
  case insufficientStorage
  case sourceChangedDuringImport
  case symbolicLinkRejected
  case unsupportedModel(String)
  case ambiguousModel(String)
  case modelNotFound(String)
  case metadataCorrupt
  case invalidDisplayName
  case immutableRevisionRequired
  case runtimeMismatch
  case modelNotLoaded
  case generationInProgress
  case promptEmpty
  case promptTooLarge
  case invalidMaxTokens
  case invalidTemperature
  case unsupportedCoreMLContract(String)
  case contextExceeded
  case inferenceFailed(String)

  var errorDescription: String? {
    switch self {
    case .unsupportedRuntime(let value):
      return "Unsupported local runtime: \(value)."
    case .invalidURI:
      return "The model URI must be a local file URL."
    case .sourceMissing:
      return "The selected model no longer exists."
    case .importTooLarge:
      return "The selected model exceeds the 16 GiB local import limit."
    case .importHasTooManyFiles:
      return "The selected model contains too many files or nested directories."
    case .insufficientStorage:
      return "The device does not have enough free space to import this model safely."
    case .sourceChangedDuringImport:
      return "The selected model changed while it was being imported."
    case .symbolicLinkRejected:
      return "Model imports cannot contain symbolic links."
    case .unsupportedModel(let detail):
      return "Unsupported model bundle: \(detail)"
    case .ambiguousModel(let detail):
      return "The model bundle is ambiguous: \(detail)"
    case .modelNotFound(let modelId):
      return "No imported model has id \(modelId)."
    case .metadataCorrupt:
      return "The local model index is unreadable."
    case .invalidDisplayName:
      return "The model display name is invalid."
    case .immutableRevisionRequired:
      return "Remote MLX models require a full 40-character commit SHA; mutable branches are rejected."
    case .runtimeMismatch:
      return "The requested runtime does not match the imported model."
    case .modelNotLoaded:
      return "Load a local model before generating."
    case .generationInProgress:
      return "A local generation is already running."
    case .promptEmpty:
      return "The local prompt cannot be empty."
    case .promptTooLarge:
      return "The local prompt is too large."
    case .invalidMaxTokens:
      return "maxTokens must be between 1 and 512."
    case .invalidTemperature:
      return "temperature must be finite and between 0 and 2."
    case .unsupportedCoreMLContract(let detail):
      return "Unsupported Core ML language-model contract: \(detail)"
    case .contextExceeded:
      return "The prompt and requested output exceed the model context window."
    case .inferenceFailed(let detail):
      return "Local inference failed: \(detail)"
    }
  }
}

enum LocalInferenceValidation {
  static func prompt(_ value: String) throws -> String {
    let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else { throw LocalInferenceError.promptEmpty }
    guard value.utf8.count <= 65_536 else { throw LocalInferenceError.promptTooLarge }
    return value
  }

  static func maxTokens(_ value: Int?) throws -> Int {
    let resolved = value ?? 256
    guard (1...512).contains(resolved) else { throw LocalInferenceError.invalidMaxTokens }
    return resolved
  }

  static func temperature(_ value: Double?) throws -> Double {
    let resolved = value ?? 0
    guard resolved.isFinite, (0...2).contains(resolved) else {
      throw LocalInferenceError.invalidTemperature
    }
    return resolved
  }

  static func immutableRevision(_ value: String?) throws -> String {
    guard let value else { throw LocalInferenceError.immutableRevisionRequired }
    let revision = value.trimmingCharacters(in: .whitespacesAndNewlines)
    guard revision.range(of: "^[0-9A-Fa-f]{40}$", options: .regularExpression) != nil else {
      throw LocalInferenceError.immutableRevisionRequired
    }
    return revision.lowercased()
  }
}
