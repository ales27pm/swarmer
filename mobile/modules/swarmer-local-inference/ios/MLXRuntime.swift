import Foundation
import HuggingFace
import MLXHuggingFace
import MLXLLM
import MLXLMCommon
import Tokenizers

actor MLXRuntime {
  private var container: ModelContainer?
  private var session: ChatSession?
  private var cancelRequested = false
  private var generating = false
  private var unloadRequested = false

  func loadLocal(directory: URL) async throws {
    guard !generating else { throw LocalInferenceError.generationInProgress }
    cancelRequested = false
    unloadRequested = false
    #if targetEnvironment(simulator)
    throw LocalInferenceError.unsupportedModel("MLX inference requires a physical iOS device")
    #else
    let loaded = try await LLMModelFactory.shared.loadContainer(
      from: directory,
      using: #huggingFaceTokenizerLoader()
    )
    guard !cancelRequested, !Task.isCancelled else { throw CancellationError() }
    container = loaded
    session = ChatSession(loaded)
    #endif
  }

  func loadRemote(modelId: String, revision: String) async throws {
    guard !generating else { throw LocalInferenceError.generationInProgress }
    cancelRequested = false
    unloadRequested = false
    #if targetEnvironment(simulator)
    throw LocalInferenceError.unsupportedModel("MLX inference requires a physical iOS device")
    #else
    let configuration = ModelConfiguration(id: modelId, revision: revision)
    let loaded = try await LLMModelFactory.shared.loadContainer(
      from: #hubDownloader(),
      using: #huggingFaceTokenizerLoader(),
      configuration: configuration,
      useLatest: false
    )
    guard !cancelRequested, !Task.isCancelled else { throw CancellationError() }
    container = loaded
    session = ChatSession(loaded)
    #endif
  }

  func generate(prompt: String, maxTokens: Int, temperature: Double) async throws -> RuntimeGenerationResult {
    guard !generating else { throw LocalInferenceError.generationInProgress }
    guard let container, let session else { throw LocalInferenceError.modelNotLoaded }
    generating = true
    cancelRequested = false
    defer {
      generating = false
      cancelRequested = false
    }

    let promptTokenCount = await container.encode(prompt).count
    guard promptTokenCount + maxTokens <= 4_096 else {
      throw LocalInferenceError.contextExceeded
    }
    if cancelRequested || Task.isCancelled {
      await finishGeneration(session: session)
      return RuntimeGenerationResult(text: "", finishReason: "cancelled", tokenCount: 0)
    }

    await session.clear()
    session.generateParameters = GenerateParameters(
      maxTokens: maxTokens,
      maxKVSize: 4_096,
      temperature: Float(temperature),
      topP: 1,
      topK: 0,
      seed: 0
    )

    var output = ""
    var completionCount: Int?
    var finishReason = "stop"
    do {
      for try await event in session.streamDetails(to: prompt) {
        if cancelRequested || Task.isCancelled {
          finishReason = "cancelled"
          break
        }
        switch event {
        case .chunk(let value):
          output += value
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
      await finishGeneration(session: session)
      throw error
    }

    await finishGeneration(session: session)
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
  }

  func unload() async {
    cancelRequested = true
    guard !generating else {
      unloadRequested = true
      return
    }
    if let session {
      await session.synchronize()
      await session.clear()
    }
    session = nil
    container = nil
  }

  private func finishGeneration(session: ChatSession) async {
    await session.synchronize()
    await session.clear()
    if unloadRequested {
      self.session = nil
      container = nil
      unloadRequested = false
    }
  }
}
