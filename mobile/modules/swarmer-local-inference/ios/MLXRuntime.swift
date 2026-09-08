import Foundation
import HuggingFace
import MLXHuggingFace
import MLXLLM
import MLXLMCommon
import Tokenizers

actor MLXRuntime {
  private var container: ModelContainer?
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
    #endif
  }

  func generate(prompt: String, maxTokens: Int, temperature: Double) async throws -> RuntimeGenerationResult {
    guard !generating else { throw LocalInferenceError.generationInProgress }
    guard let container else { throw LocalInferenceError.modelNotLoaded }
    generating = true
    cancelRequested = false
    defer {
      generating = false
      cancelRequested = false
      if unloadRequested {
        self.container = nil
        unloadRequested = false
      }
    }

    let promptTokenCount = await container.encode(prompt).count
    guard promptTokenCount + maxTokens <= 4_096 else {
      throw LocalInferenceError.contextExceeded
    }
    if cancelRequested || Task.isCancelled {
      return RuntimeGenerationResult(text: "", finishReason: "cancelled", tokenCount: 0)
    }

    // ChatSession is explicitly single-task and non-Sendable. Keep it local to this
    // generation so it cannot cross the runtime actor or race with unload().
    let session = ChatSession(
      container,
      generateParameters: GenerateParameters(
        maxTokens: maxTokens,
        maxKVSize: 4_096,
        temperature: Float(temperature),
        topP: 1,
        topK: 0,
        seed: 0
      )
    )

    var output = ""
    var completionCount: Int?
    var finishReason = "stop"
    var generationError: (any Error)?
    do {
      // Keep the stream in a nested scope. On an early break its termination
      // callback cancels the producer before synchronize() waits on the cache.
      let events = session.streamDetails(to: prompt)
      for try await event in events {
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
      generationError = error
    }

    // This is the final use of the task-local, non-Sendable session. It preserves
    // the cancellation/unload completion barrier without storing it on the actor.
    await session.synchronize()
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
  }

  func unload() {
    cancelRequested = true
    guard !generating else {
      unloadRequested = true
      return
    }
    container = nil
    unloadRequested = false
  }
}
