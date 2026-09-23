import Foundation

enum EmbeddingValidation {
  static let repository = "intfloat/multilingual-e5-small"

  // Pinned E5-small config uses a BERT encoder with an XLM-R tokenizer.
  // https://huggingface.co/intfloat/multilingual-e5-small/blob/614241f622f53c4eeff9890bdc4f31cfecc418b3/config.json
  static func validateE5Configuration(_ config: [String: Any]) throws {
    guard config["model_type"] as? String == "bert",
          config["architectures"] as? [String] == ["BertModel"],
          config["hidden_size"] as? Int == 384,
          config["intermediate_size"] as? Int == 1536,
          config["num_hidden_layers"] as? Int == 12,
          config["num_attention_heads"] as? Int == 12,
          config["max_position_embeddings"] as? Int == 512,
          config["type_vocab_size"] as? Int == 2,
          config["vocab_size"] as? Int == 250037,
          config["tokenizer_class"] as? String == "XLMRobertaTokenizer" else {
      throw ValidationError.configuration
    }
  }

  static func validate(texts: [String], kind: String) throws {
    guard kind == "query" || kind == "document" else { throw ValidationError.kind }
    guard (1...8).contains(texts.count), texts.allSatisfy({
      !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && $0.utf8.count <= 16_384
    }) else { throw ValidationError.texts }
  }

  enum ValidationError: LocalizedError {
    case kind, texts, configuration
    var errorDescription: String? {
      switch self {
      case .kind: return "Embedding kind must be query or document."
      case .texts: return "Embeddings require 1 to 8 nonempty texts of at most 16384 UTF-8 bytes each."
      case .configuration: return "This revision does not match the supported multilingual E5-small BERT architecture."
      }
    }
  }
}
