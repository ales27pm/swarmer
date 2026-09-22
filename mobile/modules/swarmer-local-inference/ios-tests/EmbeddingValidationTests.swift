import Foundation

@main
struct EmbeddingValidationTests {
  static func main() throws {
    try EmbeddingValidation.validate(texts: ["Mon rendez-vous demain"], kind: "query")
    try EmbeddingValidation.validate(texts: Array(repeating: "Document", count: 8), kind: "document")
    let invalid: [([String], String)] = [
      ([], "query"), (["  \n"], "query"), (["text"], "other"),
      (Array(repeating: "text", count: 9), "document"),
      ([String(repeating: "a", count: 16_385)], "query"),
      ([String(repeating: "😀", count: 4097)], "document"),
    ]
    for (texts, kind) in invalid {
      do {
        try EmbeddingValidation.validate(texts: texts, kind: kind)
        fatalError("Invalid embedding input was accepted")
      } catch is EmbeddingValidation.ValidationError {}
    }
    print("EmbeddingValidation: 8 cases passed")
  }
}
