import AppIntents
import Foundation
import UIKit

@available(iOS 18.0, *)
enum MonGARSDestination: String, AppEnum {
  case chat
  case tasks
  case approvals
  case memory
  case agents
  case settings

  static let typeDisplayRepresentation: TypeDisplayRepresentation = "Destination monGARS"

  static let caseDisplayRepresentations: [Self: DisplayRepresentation] = [
    .chat: "Discussion",
    .tasks: "Tâches",
    .approvals: "Accords",
    .memory: "Mémoire",
    .agents: "Agents",
    .settings: "Réglages",
  ]

  fileprivate var route: String {
    switch self {
    case .chat: "/"
    case .tasks: "/tasks"
    case .approvals: "/approvals"
    case .memory: "/memory"
    case .agents: "/agents"
    case .settings: "/settings"
    }
  }
}

@available(iOS 18.0, *)
private enum MonGARSIntentRouter {
  @MainActor
  static func open(path: String, queryItems: [URLQueryItem] = []) async throws {
    var components = URLComponents()
    components.scheme = "mongars"
    components.host = "app"
    components.path = path == "/" ? "" : path
    components.queryItems = queryItems.isEmpty ? nil : queryItems

    guard let url = components.url, await UIApplication.shared.open(url) else {
      throw MonGARSIntentError.cannotOpenApp
    }
  }
}

@available(iOS 18.0, *)
private enum MonGARSIntentError: Error, CustomLocalizedStringResourceConvertible {
  case cannotOpenApp

  var localizedStringResource: LocalizedStringResource {
    "Impossible d’ouvrir monGARS."
  }
}

@available(iOS 18.0, *)
struct OpenMonGARSDestinationIntent: AppIntent {
  static let title: LocalizedStringResource = "Ouvrir monGARS"
  static let description = IntentDescription("Ouvre une destination précise dans monGARS.")
  static let openAppWhenRun = true
  @available(iOS 26.0, *)
  static let supportedModes: IntentModes = .foreground(.immediate)

  @Parameter(title: "Destination")
  var destination: MonGARSDestination

  init() {}

  init(destination: MonGARSDestination) {
    self.destination = destination
  }

  @MainActor
  func perform() async throws -> some IntentResult & ProvidesDialog {
    try await MonGARSIntentRouter.open(path: destination.route)
    return .result(dialog: "monGARS est ouvert sur \(destination.caseDisplayRepresentationsTitle).")
  }
}

@available(iOS 18.0, *)
struct PrepareMonGARSTaskIntent: AppIntent {
  static let title: LocalizedStringResource = "Préparer une tâche monGARS"
  static let description = IntentDescription(
    "Préremplit une intention dans monGARS sans la lancer automatiquement."
  )
  static let openAppWhenRun = true
  @available(iOS 26.0, *)
  static let supportedModes: IntentModes = .foreground(.immediate)

  @Parameter(
    title: "Intention",
    description: "Ce que le swarm doit préparer."
  )
  var intention: String

  init() {}

  @MainActor
  func perform() async throws -> some IntentResult & ProvidesDialog {
    let trimmed = intention.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else {
      return .result(dialog: "L’intention est vide; aucune tâche n’a été préparée.")
    }
    try await MonGARSIntentRouter.open(
      path: "/",
      queryItems: [
        URLQueryItem(name: "draft", value: String(trimmed.prefix(8_000))),
        URLQueryItem(name: "intentMode", value: "task"),
      ]
    )
    return .result(dialog: "L’intention est prête dans monGARS. Confirme-la dans l’app.")
  }
}

@available(iOS 18.0, *)
struct MonGARSAppShortcuts: AppShortcutsProvider {
  static var appShortcuts: [AppShortcut] {
    AppShortcut(
      intent: OpenMonGARSDestinationIntent(destination: .chat),
      phrases: [
        "Ouvrir la discussion dans \(.applicationName)",
        "Discuter avec \(.applicationName)",
      ],
      shortTitle: "Discuter",
      systemImageName: "bubble.left.and.bubble.right"
    )
    AppShortcut(
      intent: PrepareMonGARSTaskIntent(),
      phrases: [
        "Préparer une tâche dans \(.applicationName)",
        "Donner une tâche à \(.applicationName)",
      ],
      shortTitle: "Préparer une tâche",
      systemImageName: "checklist"
    )
    AppShortcut(
      intent: OpenMonGARSDestinationIntent(destination: .approvals),
      phrases: [
        "Voir les accords dans \(.applicationName)",
      ],
      shortTitle: "Voir les accords",
      systemImageName: "checkmark.shield"
    )
  }
}

@available(iOS 18.0, *)
private extension MonGARSDestination {
  var caseDisplayRepresentationsTitle: LocalizedStringResource {
    switch self {
    case .chat: "Discussion"
    case .tasks: "Tâches"
    case .approvals: "Accords"
    case .memory: "Mémoire"
    case .agents: "Agents"
    case .settings: "Réglages"
    }
  }
}
