/** Public outcome codes only; prose is never heuristically classified as a refusal. */
const WRITING_OUTCOMES: Record<string, { label: string; description: string }> = {
  writing_needs_clarification: {
    label: "Précision demandée",
    description: "L’agent attend une réponse à sa question. Ouvre la conversation du projet pour préciser la demande ; les résultats enregistrés sont conservés.",
  },
  writing_insufficient_sources: {
    label: "Sources insuffisantes",
    description: "Les sources disponibles ne permettent pas de produire le document demandé. Précise les sources ou demande une recherche complémentaire dans la conversation du projet.",
  },
  writing_requirements_unmet: {
    label: "Document non conforme",
    description: "La rédaction ne respecte pas toutes les exigences demandées. Consulte les contrôles et précise la correction dans la conversation du projet.",
  },
  document_requirement_unverifiable: {
    label: "Conformité du document non établie",
    description: "Les livrables enregistrés ne permettent pas de confirmer les exigences du document demandé. Consulte les résultats et précise le document à vérifier ; les fichiers enregistrés sont conservés.",
  },
  writing_evidence_invalid: {
    label: "Preuve de rédaction invalide",
    description: "Le reçu du document ne correspond pas aux données attendues. Actualise les preuves et consulte le diagnostic ; aucune conformité n’est confirmée.",
  },
  writing_budget_exceeded: {
    label: "Limite de rédaction atteinte",
    description: "Le document demandé dépasse la limite de cette rédaction. Propose un découpage en plusieurs documents dans la conversation du projet.",
  },
  model_declined: {
    label: "Refus déclaré par le modèle",
    description: "Le modèle a déclaré un refus. Sa réponse est conservée dans la conversation du projet ; aucune réussite n’est confirmée.",
  },
};

export function workOutcome(reason?: string | null) {
  return reason && Object.prototype.hasOwnProperty.call(WRITING_OUTCOMES, reason) ? WRITING_OUTCOMES[reason] : undefined;
}
