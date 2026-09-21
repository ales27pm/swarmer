// Compatibility with the persisted diagnostic from project-worker schema 1.0.
// Match the complete runtime-owned text, never arbitrary model/user timeout prose.
// This affects presentation only: question IDs, replies, and approval rules stay
// authoritative on the server. Unknown diagnostics retain the clarification UI.
export const PROJECT_MODEL_TIMEOUT_PAUSE_DIAGNOSTIC =
  "The local model timed out twice without producing an accepted edit, so the project is "
  + "paused with its files and check receipts unchanged instead of consuming more model-call "
  + "budget. Send a project message when you want to resume.";

export function projectPausePresentation(message: string) {
  if (message === PROJECT_MODEL_TIMEOUT_PAUSE_DIAGNOSTIC) {
    return {
      kind: "model_timeout" as const,
      label: "Pause technique du modèle",
      notice: "Le modèle a dépassé son délai deux fois sans modification acceptée. Le projet est en pause ; les fichiers et les résultats de vérification sont conservés. Envoyez un message pour demander la reprise. L’application des fichiers dans votre espace de travail nécessitera une approbation distincte.",
      inputLabel: "Message pour reprendre le projet",
      placeholder: "Votre message de reprise…",
      submitLabel: "Envoyer au projet",
    };
  }
  return {
    kind: "clarification" as const,
    label: "Votre réponse est attendue",
    notice: "Une précision est demandée. Votre réponse sera liée à cette question et permettra de poursuivre le travail sur le projet. L’application des fichiers dans votre espace de travail nécessitera une approbation distincte.",
    inputLabel: "Réponse à la question du projet",
    placeholder: "Votre réponse…",
    submitLabel: "Répondre à la question",
  };
}
