import Foundation

enum LocalRuntime: String, Codable, Sendable {
  case coreML = "coreml"
  case mlx
  case llamaCpp = "llama.cpp"
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

enum LocalInferenceError: Error, Sendable {
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
}
