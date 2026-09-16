import Foundation
import HuggingFace
import MLX
import MLXHuggingFace
import MLXLLM
import MLXLMCommon
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
    onOutputProgress: (@Sendable (Int) async -> Void)? = nil
  ) async throws -> RuntimeGenerationResult {
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
      // synchronize() below settles the producer before cancelled/completed
      // generation returns; never clear cached buffers from cancel() mid-stream.
      Memory.clearCache()
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
    var outputBytes = 0
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
    Memory.clearCache()
  }
}
