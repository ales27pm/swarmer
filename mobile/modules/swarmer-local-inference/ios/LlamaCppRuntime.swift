import Foundation

private final class LlamaBridgeBox: @unchecked Sendable {
  // Safety invariant: SwarmerLlamaBridge serializes model/context access internally and exposes
  // cancellation through an atomic flag. The box never replaces its bridge instance.
  let bridge = SwarmerLlamaBridge()
}

private struct LlamaWireResult: Decodable, Sendable {
  let text: String
  let finishReason: String
  let tokenCount: Int
}

actor LlamaCppRuntime {
  private let box = LlamaBridgeBox()
  private var generating = false

  func load(modelURL: URL) async throws {
    guard !generating else { throw LocalInferenceError.generationInProgress }
    guard modelURL.pathExtension.lowercased() == "gguf" else {
      throw LocalInferenceError.unsupportedModel("llama.cpp accepts only GGUF files")
    }
    #if targetEnvironment(simulator)
    throw LocalInferenceError.unsupportedModel(
      "the pinned llama.cpp XCFramework does not contain an iOS Simulator slice"
    )
    #else
    let box = box
    box.bridge.resetCancellation()
    let task = Task.detached(priority: .userInitiated) {
      try box.bridge.loadModel(atPath: modelURL.path, contextSize: 4_096)
    }
    try await withTaskCancellationHandler {
      try await task.value
    } onCancel: {
      box.bridge.cancel()
      task.cancel()
    }
    #endif
  }

  func generate(prompt: String, maxTokens: Int, temperature: Double) async throws -> RuntimeGenerationResult {
    guard !generating else { throw LocalInferenceError.generationInProgress }
    guard box.bridge.isLoaded else { throw LocalInferenceError.modelNotLoaded }
    generating = true
    defer { generating = false }
    let box = box
    box.bridge.resetCancellation()

    let task = Task.detached(priority: .userInitiated) {
        try box.bridge.generatePrompt(prompt, maxTokens: maxTokens, temperature: temperature)
    }
    let json: String = try await withTaskCancellationHandler {
      try await task.value
    } onCancel: {
      box.bridge.cancel()
      task.cancel()
    }
    let decoded = try JSONDecoder().decode(LlamaWireResult.self, from: Data(json.utf8))
    guard ["stop", "length", "cancelled"].contains(decoded.finishReason), decoded.tokenCount >= 0 else {
      throw LocalInferenceError.inferenceFailed("llama.cpp returned an invalid result envelope")
    }
    return RuntimeGenerationResult(
      text: decoded.text,
      finishReason: decoded.finishReason,
      tokenCount: decoded.tokenCount
    )
  }

  func cancel() {
    box.bridge.cancel()
  }

  func unload() async {
    box.bridge.cancel()
    let box = box
    await Task.detached(priority: .utility) {
      box.bridge.unload()
    }.value
  }
}
