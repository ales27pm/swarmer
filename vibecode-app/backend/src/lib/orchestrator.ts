import { db, id, now } from "../db";
import { appendAudit } from "./audit";
import type { Task } from "../types";

// Simulated orchestrator — deterministic stand-in for the abliterated LLM
// (MASTER_SPEC: "Les agents doivent être testables avec fake model output").
// The pipeline: queued → running → (waiting_permission if sensitive) → completed.

const ORCHESTRATOR_ID = "orchestrator-01";
const CODE_WORKER_ID = "code-worker-01";

const SENSITIVE_RE = /\b(écri|ecri|write|modif|patch|appliqu|apply|delete|supprim|remplac|corrige|fix|change)\b/i;
const LIST_RE = /\b(liste|list|fichiers|files|ls\b|arborescence)/i;

function getTask(taskId: string): Task | null {
  return db.query("SELECT * FROM tasks WHERE id = ?").get(taskId) as Task | null;
}

function setStatus(taskId: string, status: string, completed = false) {
  db.prepare("UPDATE tasks SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?").run(
    status,
    now(),
    completed ? now() : null,
    taskId
  );
}

function postMessage(task: Task, role: string, agentId: string | null, content: string) {
  if (!task.conversation_id) return;
  db.prepare(
    `INSERT INTO messages (id, conversation_id, task_id, role, agent_id, content, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  ).run(id("msg"), task.conversation_id, task.id, role, agentId, content, now());
  db.prepare("UPDATE conversations SET updated_at = ? WHERE id = ?").run(now(), task.conversation_id);
}

function fakeFileListing(): string {
  return [
    "Voici l'arborescence du projet (lecture sandboxée):",
    "",
    "27pm-crm/",
    "  src/",
    "    api/client.ts",
    "    api/tasks.ts",
    "    components/TaskCard.tsx",
    "    screens/Dashboard.tsx",
    "  package.json",
    "  README.md",
    "",
    "6 fichiers inspectés. Aucune modification effectuée.",
  ].join("\n");
}

function buildPlan(input: string): string {
  return [
    "Plan proposé:",
    "1. Récupérer le contexte du projet et la mémoire pertinente.",
    "2. Inspecter les fichiers autorisés en lecture seule.",
    "3. Produire un rapport ou un diff selon la demande.",
    "4. Demander permission avant toute écriture.",
    "",
    `Demande: « ${input.slice(0, 140)} »`,
  ].join("\n");
}

export function runTaskPipeline(taskId: string) {
  const task = getTask(taskId);
  if (!task || task.status !== "queued") return;

  // Step 1 — orchestrator picks up the task and emits a plan.
  setTimeout(() => {
    const t = getTask(taskId);
    if (!t || t.status === "cancelled") return;
    setStatus(taskId, "running");
    appendAudit({
      trace_id: taskId,
      event_type: "task.started",
      actor_type: "agent",
      actor_id: ORCHESTRATOR_ID,
      task_id: taskId,
      payload: { mode: t.mode },
    });
    postMessage(t, "agent", ORCHESTRATOR_ID, buildPlan(t.input));

    // Step 2 — sensitive actions go through the Permission Gateway.
    setTimeout(() => {
      const t2 = getTask(taskId);
      if (!t2 || t2.status === "cancelled") return;

      if (SENSITIVE_RE.test(t2.input) && t2.mode !== "review") {
        const approvalId = id("apr");
        const target = "/home/ales27pm/projects/27pm-crm/src/api/client.ts";
        db.prepare(
          `INSERT INTO approvals (id, task_id, agent_id, action_type, target, reason, risk, status, request_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)`
        ).run(
          approvalId,
          taskId,
          CODE_WORKER_ID,
          "write_file",
          target,
          `Appliquer le patch demandé: « ${t2.input.slice(0, 120)} »`,
          "medium",
          JSON.stringify({ tool: "write_file", diff_preview: "@@ client.ts\n- const res = fetch(url)\n+ const res = await fetch(url)" }),
          now()
        );
        setStatus(taskId, "waiting_permission");
        appendAudit({
          trace_id: taskId,
          event_type: "approval.requested",
          actor_type: "system",
          actor_id: "permission-gateway",
          task_id: taskId,
          payload: { approval_id: approvalId, action_type: "write_file", risk: "medium" },
        });
        postMessage(
          t2,
          "agent",
          CODE_WORKER_ID,
          "Action sensible détectée (write_file). La Permission Gateway bloque l'exécution jusqu'à ton approbation — voir l'onglet Approbations."
        );
        return;
      }

      completeTask(taskId, LIST_RE.test(t2.input) ? fakeFileListing() : defaultResult(t2.input));
    }, 1500);
  }, 800);
}

function defaultResult(input: string): string {
  return [
    "Tâche terminée (simulation locale).",
    "",
    `Analyse de: « ${input.slice(0, 140)} »`,
    "",
    "Résultat: contexte récupéré, mémoire consultée, aucune action sensible requise. Le worker sandboxé n'a rien modifié.",
  ].join("\n");
}

export function completeTask(taskId: string, result: string) {
  const t = getTask(taskId);
  if (!t || t.status === "cancelled") return;
  setStatus(taskId, "completed", true);
  appendAudit({
    trace_id: taskId,
    event_type: "task.completed",
    actor_type: "agent",
    actor_id: CODE_WORKER_ID,
    task_id: taskId,
    payload: { result_preview: result.slice(0, 200) },
  });
  postMessage(t, "agent", CODE_WORKER_ID, result);
}

export function failTask(taskId: string, reason: string) {
  const t = getTask(taskId);
  if (!t) return;
  db.prepare("UPDATE tasks SET status = 'failed', updated_at = ?, completed_at = ?, error_json = ? WHERE id = ?").run(
    now(),
    now(),
    JSON.stringify({ reason }),
    taskId
  );
  appendAudit({
    trace_id: taskId,
    event_type: "task.failed",
    actor_type: "system",
    actor_id: "orchestrator",
    task_id: taskId,
    payload: { reason },
  });
  postMessage(t, "system", null, `Tâche interrompue: ${reason}`);
}

// Called by the approvals route after a human decision (Permission Gateway).
export function resolveApproval(approvalId: string, decision: "allow_once" | "allow_rule" | "deny", userNote?: string) {
  const approval = db.query("SELECT * FROM approvals WHERE id = ?").get(approvalId) as
    | { id: string; task_id: string; status: string }
    | null;
  if (!approval || approval.status !== "pending") return null;

  db.prepare("UPDATE approvals SET status = ?, decision_json = ?, decided_at = ? WHERE id = ?").run(
    decision === "deny" ? "denied" : "allowed",
    JSON.stringify({ decision, user_note: userNote ?? null }),
    now(),
    approvalId
  );
  appendAudit({
    trace_id: approval.task_id,
    event_type: `approval.${decision}`,
    actor_type: "user",
    actor_id: "iphone",
    task_id: approval.task_id,
    payload: { approval_id: approvalId, user_note: userNote ?? null },
  });

  if (decision === "deny") {
    failTask(approval.task_id, "Permission refusée par l'utilisateur.");
  } else {
    setTimeout(() => {
      completeTask(
        approval.task_id,
        "Permission accordée. Patch appliqué dans le sandbox, tests simulés passés (12/12). Diff enregistré dans le journal d'audit."
      );
    }, 1200);
  }
  return db.query("SELECT * FROM approvals WHERE id = ?").get(approvalId);
}
