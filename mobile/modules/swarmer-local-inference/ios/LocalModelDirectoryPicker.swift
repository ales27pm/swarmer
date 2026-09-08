import Foundation
import UIKit
import UniformTypeIdentifiers

enum LocalModelDirectorySelection: Sendable {
  case selected(URL)
  case cancelled
}

@MainActor
final class LocalModelDirectoryPicker: NSObject, UIDocumentPickerDelegate,
  UIAdaptivePresentationControllerDelegate
{
  private var completion: (@Sendable (LocalModelDirectorySelection) -> Void)?
  private(set) var isActive = true

  init(completion: @escaping @Sendable (LocalModelDirectorySelection) -> Void) {
    self.completion = completion
  }

  func present(from viewController: UIViewController) {
    let picker = UIDocumentPickerViewController(
      forOpeningContentTypes: [UTType.folder],
      asCopy: false
    )
    picker.delegate = self
    picker.presentationController?.delegate = self
    picker.allowsMultipleSelection = false

    if UIDevice.current.userInterfaceIdiom == .pad {
      let frame = viewController.view.frame
      picker.popoverPresentationController?.sourceRect = CGRect(
        x: frame.midX,
        y: frame.maxY,
        width: 0,
        height: 0
      )
      picker.popoverPresentationController?.sourceView = viewController.view
      picker.modalPresentationStyle = .pageSheet
    }
    viewController.present(picker, animated: true)
  }

  func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
    guard let url = urls.first else {
      finish(with: .cancelled)
      return
    }
    finish(with: .selected(url))
  }

  func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
    finish(with: .cancelled)
  }

  func presentationControllerDidDismiss(_ presentationController: UIPresentationController) {
    finish(with: .cancelled)
  }

  private func finish(with result: LocalModelDirectorySelection) {
    guard isActive else { return }
    isActive = false
    let callback = completion
    completion = nil
    callback?(result)
  }
}
