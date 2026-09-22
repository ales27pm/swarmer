import Foundation

enum EmbeddingValidation {
  static func validate(texts: [String], kind: String) throws {
    guard kind == "query" || kind == "document" else { throw ValidationError.kind }
    guard (1...8).contains(texts.count), texts.allSatisfy({
      !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && $0.utf8.count <= 16_384
    }) else { throw ValidationError.texts }
  }

  enum ValidationError: LocalizedError {
    case kind, texts
    var errorDescription: String? {
      switch self {
      case .kind: return "Embedding kind must be query or document."
      case .texts: return "Embeddings require 1 to 8 nonempty texts of at most 16384 UTF-8 bytes each."
      }
    }
  }
}
