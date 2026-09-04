# 01 — Product Vision

## Nom provisoire

**monGARS Swarm App**

## Phrase claire

Une application iPhone qui pilote ton propre système d'agents IA local: les agents pensent, se coordonnent, utilisent ta mémoire, demandent les permissions nécessaires, puis exécutent sur tes machines.

## Utilisateur principal

- Toi, ales27pm.
- Usage solo au départ.
- Objectif: assistant personnel/technique local-first, puissant, hackable, contrôlé.

## Problème

Les assistants classiques restent coincés dans le chat. Ils ne connaissent pas assez ton contexte, ne peuvent pas agir correctement sur tes machines, oublient les projets, refusent trop souvent sans proposer de chemin utile et ne donnent pas un vrai contrôle local.

## Solution

Créer une app iPhone connectée à un control plane Ubuntu local qui héberge:

- un orchestrateur LLM;
- un swarm d'agents spécialisés;
- un state partagé;
- une mémoire sémantique longue durée;
- une passerelle de permissions;
- un système de feedback et d'amélioration continue.

## Promesse

Tu peux parler à ton iPhone et dire:

> “Reprends mon projet 27PM CRM, regarde le dernier état, trouve les erreurs, propose un patch, demande-moi permission avant d'écrire.”

Le système:

1. récupère le contexte;
2. route vers les bons agents;
3. consulte les fichiers/mémoire/state;
4. prépare un plan;
5. demande permission si action sensible;
6. exécute;
7. logue;
8. apprend du résultat.

## Principes d'expérience

- Pas de sermons inutiles.
- Pas de magie opaque.
- Toujours un état visible.
- Toujours une permission claire avant action sensible.
- Les agents doivent expliquer ce qu'ils demandent, mais ne doivent pas jouer au gardien moral.
- L'app doit se sentir locale même quand Ubuntu est la source de vérité.

## Positions produit

### Mode normal

L'utilisateur donne une intention; l'orchestrateur planifie; la gateway demande permission quand nécessaire.

### Mode commandant

L'utilisateur donne une action précise; le système minimise la discussion et exécute après validation si requis.

### Mode review

Le système inspecte, critique, propose, mais ne modifie rien.

### Mode autonome surveillé

Le système avance une tâche longue par étapes, mais chaque palier risqué déclenche une demande d'approbation.

## Ce que ça ouvre

- Assistant iPhone qui pilote Ubuntu.
- Swarm de coding agents locaux.
- Mémoire personnelle/projets persistante.
- Agents qui demandent des infos au iPhone: position, photos, calendrier, contacts, etc.
- Automatisation de projets 27PM.
- Audit/rewrite de code.
- Gestion CRM, rappels, courriels et calendrier via bridges autorisés.
- Dataset d'évaluation et fine-tuning progressif.
- Extension à d'autres machines plus tard.
