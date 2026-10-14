import Foundation

/// Cross-repository contract probe. Payloads are synthetic, never Core ML inference.
@main
struct DolphinManifestImportTests {
  static func main() {
    do { try inspect() }
    catch {
      print("rejected: \(error.localizedDescription)")
      exit(1)
    }
  }

  static func inspect() throws {
    guard CommandLine.arguments.count == 2 else {
      throw DolphinCoreMLBundleError.invalid("expected fixture directory")
    }
    let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
    let bundle = try DolphinCoreMLBundle.inspect(directory: directory)
    guard bundle.modelURLs.count == 9,
          bundle.sidecarURLs.contains(where: { $0.lastPathComponent == "manifest.json" }) else {
      throw DolphinCoreMLBundleError.invalid("incomplete import selection")
    }
    print("accepted schema \(bundle.manifest.formatVersion); 9 model directories; source declaration only; synthetic payloads")
  }
}
