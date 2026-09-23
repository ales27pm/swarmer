import Foundation

enum LocalModelPurpose: String, Codable, Sendable {
  case generation
  case embeddings

  static func mlx(configuration: [String: Any]) -> Self {
    let encoders: Set<String> = ["bert", "roberta", "xlm-roberta", "distilbert", "nomic_bert"]
    return encoders.contains(configuration["model_type"] as? String ?? "") ? .embeddings : .generation
  }

  static func requireGeneration(configuration: [String: Any]) throws {
    guard mlx(configuration: configuration) == .generation else {
      throw LocalInferenceError.unsupportedModel("This is an embedding encoder. Load it through the semantic-memory embeddings controls, not language generation.")
    }
  }
}

extension StoredLocalModel {
  var effectivePurpose: LocalModelPurpose {
    // Old E5 records and their Documents manifests have no purpose field.
    // Interpret their immutable origin without rewriting either record or weights.
    if remoteOrigin?.repositoryId == EmbeddingValidation.repository { return .embeddings }
    return purpose ?? .generation
  }
}
