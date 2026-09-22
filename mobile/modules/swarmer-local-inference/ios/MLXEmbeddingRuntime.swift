import Foundation
import HuggingFace
import MLX
import MLXEmbedders
import MLXHuggingFace
import MLXLMCommon
import Tokenizers

private struct PreparedEmbeddingTokenizer: MLXLMCommon.TokenizerLoader {
  let directory: URL
  let tokenizer: any MLXLMCommon.Tokenizer
  func load(from directory: URL) async throws -> any MLXLMCommon.Tokenizer {
    guard directory.standardizedFileURL == self.directory.standardizedFileURL else {
      throw LocalInferenceError.unsupportedModel("embedding tokenizer directory changed")
    }
    return tokenizer
  }
}

actor MLXEmbeddingRuntime {
  static let repository = "intfloat/multilingual-e5-small"
  static let pipeline = "e5-prefixes-mean-l2-specialtokens-v1"
  private var container: EmbedderModelContainer?
  private var operation: Task<EmbedderModelContainer, Error>?
  private var embeddingOperation: Task<EmbeddingResultRecord, Error>?
  private var epoch = 0
  private var state = "disabled"
  private var revision: String?
  private var message: String?

  func status() -> EmbeddingStatusRecord {
    EmbeddingStatusRecord(state: state, modelId: revision == nil ? nil : Self.repository,
      revision: revision, message: message)
  }

  func load(options: LoadEmbedderOptions, store: LocalModelStore) async throws -> EmbeddingStatusRecord {
    guard options.experimental else { throw LocalInferenceError.unsupportedModel("Enable experimental local embeddings explicitly") }
    guard state != "unloading", operation == nil, embeddingOperation == nil, container == nil else { throw LocalInferenceError.generationInProgress }
    let immutableRevision = try LocalInferenceValidation.immutableRevision(options.revision)
    guard options.modelId == Self.repository else {
      throw LocalInferenceError.unsupportedModel("Experimental embeddings currently support intfloat/multilingual-e5-small only")
    }
    #if targetEnvironment(simulator)
    throw LocalInferenceError.unsupportedModel("MLX embeddings require a physical iOS device")
    #else
    epoch += 1
    let loadEpoch = epoch
    revision = immutableRevision
    state = "loading"
    message = nil
    let task = Task.detached(priority: .utility) {
      let resolved: ResolvedLocalModel
      if let stored = try await store.resolveRemoteMLX(repositoryId: Self.repository, revision: immutableRevision) {
        resolved = stored
      } else {
        let repo = Repo.ID(namespace: "intfloat", name: "multilingual-e5-small")
        let client = HubClient()
        guard let cache = client.cache else { throw LocalInferenceError.modelDownloadFailed }
        let snapshot = try await client.downloadSnapshot(of: repo, revision: immutableRevision,
          matching: ["*.json", "*.safetensors", "*.model", "*.txt", "*.jinja", "*.tiktoken"])
        try Task.checkCancellation()
        resolved = try await store.preserveMLXSnapshot(at: snapshot,
          repositoryCacheURL: cache.repoDirectory(repo: repo, kind: .model),
          repositoryId: Self.repository, revision: immutableRevision)
      }
      try Task.checkCancellation()
      Memory.cacheLimit = 20 * 1024 * 1024
      let directory = resolved.runtimeURL
      let config = try JSONSerialization.jsonObject(with: Data(contentsOf: directory.appendingPathComponent("config.json"))) as? [String: Any]
      guard config?["model_type"] as? String == "xlm-roberta", config?["hidden_size"] as? Int == 384 else {
        throw LocalInferenceError.unsupportedModel("This revision does not match the E5-small architecture")
      }
      // Prepare tokenizer before evaluating weights, matching the generation runtime's bounded load path.
      let tokenizer = try await #huggingFaceTokenizerLoader().load(from: directory)
      let loader = PreparedEmbeddingTokenizer(directory: directory, tokenizer: tokenizer)
      let loaded = try await MLX.withError {
        try await EmbedderModelFactory.shared.loadContainer(from: directory, using: loader)
      }
      try Task.checkCancellation()
      return loaded
    }
    operation = task
    do {
      let loaded = try await task.value
      guard epoch == loadEpoch else { throw CancellationError() }
      operation = nil
      container = loaded
      state = "ready"
      return status()
    } catch {
      if epoch == loadEpoch {
        operation = nil
        state = "failed"
        message = error.localizedDescription
      }
      throw error
    }
    #endif
  }

  func embed(options: EmbedOptions) async throws -> EmbeddingResultRecord {
    guard let container, let revision, state == "ready", embeddingOperation == nil else {
      throw LocalInferenceError.modelNotLoaded
    }
    try EmbeddingValidation.validate(texts: options.texts, kind: options.kind)
    let texts = options.texts
    let kind = options.kind
    let inferenceEpoch = epoch
    state = "embedding"
    let task = Task.detached(priority: .utility) {
      try await container.perform { context in
        var vectors: [[Float]] = []
        var counts: [Int] = []
        for text in texts {
          try Task.checkCancellation()
          let prefix = kind == "query" ? "query: " : "passage: "
          let tokens = context.tokenizer.encode(text: prefix + text, addSpecialTokens: true)
          guard !tokens.isEmpty, tokens.count <= 512 else {
            throw LocalInferenceError.unsupportedModel("Embedding input exceeds E5's 512-token context; split the source first")
          }
          let values: [Float] = try MLX.withError {
            let input = MLXArray(tokens).expandedDimensions(axis: 0)
            let mask = MLXArray.ones(like: input)
            let output = context.model(input, positionIds: nil,
              tokenTypeIds: MLXArray.zeros(like: input), attentionMask: mask)
            // The fixed E5 profile defines mean pooling explicitly; the durable store intentionally keeps direct sidecars only.
            let pooled = Pooling(strategy: .mean)(output, mask: mask, normalize: true, applyLayerNorm: false)
            eval(pooled)
            guard pooled.shape == [1, 384] else {
              throw LocalInferenceError.inferenceFailed("Unexpected E5 embedding dimensions")
            }
            return pooled.asArray(Float.self)
          }
          guard values.allSatisfy(\.isFinite) else { throw LocalInferenceError.inferenceFailed("Embedding contains nonfinite values") }
          vectors.append(values)
          counts.append(tokens.count)
        }
        try Task.checkCancellation()
        return EmbeddingResultRecord(modelId: Self.repository, revision: revision,
          kind: kind, vectors: vectors, tokenCounts: counts)
      }
    }
    embeddingOperation = task
    do {
      let result = try await task.value
      guard epoch == inferenceEpoch else { throw CancellationError() }
      embeddingOperation = nil
      state = "ready"
      return result
    } catch {
      if epoch == inferenceEpoch {
        embeddingOperation = nil
        state = "ready"
        message = error.localizedDescription
      }
      throw error
    }
  }

  func unload() async {
    epoch += 1
    let unloadEpoch = epoch
    state = "unloading"
    let pendingLoad = operation
    let pendingEmbedding = embeddingOperation
    pendingLoad?.cancel()
    pendingEmbedding?.cancel()
    if let pendingLoad { _ = await pendingLoad.result }
    if let pendingEmbedding { _ = await pendingEmbedding.result }
    guard epoch == unloadEpoch else { return }
    operation = nil
    embeddingOperation = nil
    container = nil
    revision = nil
    message = nil
    state = "disabled"
  }
}
