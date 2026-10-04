import Foundation

/// The first supported ANEMLL pipeline is deliberately narrow. Component names
/// identify a layout, not proof that its graph can load or execute on the ANE.
enum ANEMLLModelProfile {
  static let repository = "anemll/anemll-Llama-3.2-1B-FAST-iOS_0.3.0"
  static let revision = "c6461a77a6f803424ec347f9537aadac37094879"
  static let componentNames: Set<String> = [
    "llama_embeddings_lut8.mlmodelc", "llama_lm_head_lut8.mlmodelc",
    "llama_FFN_PF_lut4_chunk_01of01.mlmodelc"
  ]
  static let compiledFiles: Set<String> = [
    "analytics/coremldata.bin", "coremldata.bin", "metadata.json", "model.mil", "weights/weight.bin"
  ]
  static let requiredSidecars: Set<String> = ["config.json", "tokenizer.json", "tokenizer_config.json"]

  static func root(for models: [URL]) -> URL? {
    guard models.count == componentNames.count,
          Set(models.map(\.lastPathComponent)) == componentNames,
          Set(models.map { $0.deletingLastPathComponent().standardizedFileURL.path }).count == 1 else { return nil }
    return models[0].deletingLastPathComponent()
  }

  static func validateDirectory(_ root: URL, fileManager: FileManager = .default) throws {
    for component in componentNames {
      let directory = root.appendingPathComponent(component, isDirectory: true)
      let values = try? directory.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
      guard values?.isDirectory == true, values?.isSymbolicLink != true,
            let enumerator = fileManager.enumerator(at: directory,
              includingPropertiesForKeys: [.isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey]) else {
        throw LocalInferenceError.unsupportedModel("incomplete ANEMLL component")
      }
      var actual = Set<String>()
      var entries = 0
      for case let item as URL in enumerator {
        entries += 1
        guard entries <= 16 else { throw LocalInferenceError.unsupportedModel("unexpected ANEMLL component contents") }
        let properties = try item.resourceValues(forKeys: [.isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
        guard properties.isSymbolicLink != true else { throw LocalInferenceError.symbolicLinkRejected }
        if properties.isRegularFile == true {
          let prefix = directory.path + "/"
          guard item.path.hasPrefix(prefix), (properties.fileSize ?? 0) > 0 else {
            throw LocalInferenceError.unsupportedModel("invalid ANEMLL component file")
          }
          actual.insert(String(item.path.dropFirst(prefix.count)))
        } else if properties.isDirectory != true {
          throw LocalInferenceError.unsupportedModel("invalid ANEMLL component contents")
        }
      }
      guard actual == compiledFiles else { throw LocalInferenceError.unsupportedModel("incomplete ANEMLL component") }
    }
    for name in requiredSidecars {
      let values = try? root.appendingPathComponent(name).resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
      guard values?.isRegularFile == true, values?.isSymbolicLink != true, (values?.fileSize ?? 0) > 0 else {
        throw LocalInferenceError.unsupportedModel("ANEMLL requires config and tokenizer sidecars")
      }
    }
  }

  /// Called only after generic path, duplicate, hash and size validation.
  static func downloadPaths(paths: [String], repoId: String, sourceRevision: String,
                            sidecars: Set<String>) throws -> [String]? {
    let compiled = paths.filter { $0.split(separator: "/").contains { $0.hasSuffix(".mlmodelc") } }
    guard !compiled.isEmpty else { return nil }
    guard repoId == repository, sourceRevision.lowercased() == revision else {
      throw LocalInferenceError.invalidDownloadMetadata
    }
    var componentRoots = Set<String>()
    for path in compiled {
      let parts = path.split(separator: "/")
      guard let index = parts.firstIndex(where: { $0.hasSuffix(".mlmodelc") }),
            componentNames.contains(String(parts[index])), index < parts.count - 1 else {
        throw LocalInferenceError.invalidDownloadMetadata
      }
      componentRoots.insert(parts.prefix(index + 1).joined(separator: "/"))
    }
    let parents = Set(componentRoots.map { ($0 as NSString).deletingLastPathComponent })
    guard componentRoots.count == 3, parents.count == 1, let parent = parents.first else {
      throw LocalInferenceError.invalidDownloadMetadata
    }
    let prefix = parent.isEmpty ? "" : parent + "/"
    guard paths.allSatisfy({ $0.hasPrefix(prefix) }) else { throw LocalInferenceError.invalidDownloadMetadata }
    let local = paths.map { String($0.dropFirst(prefix.count)) }
    let expectedCompiled = Set(componentNames.flatMap { name in compiledFiles.map { name + "/" + $0 } })
    let actualCompiled = Set(local.filter { $0.contains("/") })
    let actualSidecars = Set(local.filter { !$0.contains("/") })
    guard actualCompiled == expectedCompiled, actualSidecars.isSubset(of: sidecars),
          actualSidecars.isSuperset(of: requiredSidecars) else { throw LocalInferenceError.invalidDownloadMetadata }
    return local.map { "model/" + $0 }
  }
}
