import type { ActivityCatalog } from "@/lib/api/activity-catalog";

export const activityCatalogFixture: ActivityCatalog = {
  schema_version: "1.0", generated_at: "2026-09-14T04:00:00Z",
  domains: [
    { id: "software", title: "Développement logiciel", description: "Sources et tests." },
    { id: "personal", title: "Agenda et quotidien", description: "Organisation personnelle." },
  ],
  skills: [
    { id: "code.build_project", title: "Construire une application", description: "Développer un projet Python ou Node.",
      inputs: ["Objectif du projet"], output: "Sources et résultats des tests", requirements: [],
      execution: { kind: "worker", target: "code.build_project" },
      availability: { state: "goal_ready", agent_ids: ["agent_project"], reason: "Un agent compatible est disponible." } },
    { id: "iphone.calendar.events", title: "Consulter le calendrier", description: "Lire les événements sélectionnés.",
      inputs: ["Période"], output: "Événements", requirements: ["Accord sur l’iPhone"],
      execution: { kind: "iphone", target: "iphone.calendar.events" },
      availability: { state: "iphone_request", agent_ids: [], reason: "Une demande explicite sur l’iPhone est nécessaire." } },
    { id: "personal.week", title: "Organiser une semaine", description: "Proposer un horaire selon les priorités.",
      inputs: ["Disponibilités"], output: "Proposition de semaine", requirements: ["Intégration de la gestion des tâches"],
      execution: { kind: "planned", target: null },
      availability: { state: "planned", agent_ids: [], reason: "Intégration à développer." } },
  ],
  roles: [
    { id: "developer", domain_id: "software", title: "Développeur de projet", description: "Réaliser une application.",
      skill_ids: ["code.build_project"], examples: ["Créer un catalogue de livres avec recherche."] },
    { id: "organizer", domain_id: "personal", title: "Organisateur personnel", description: "Préparer le quotidien.",
      skill_ids: ["iphone.calendar.events", "personal.week"], examples: ["Préparer mon agenda de la semaine."] },
  ],
};
