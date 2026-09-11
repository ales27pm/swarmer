export type GoalCodeProposal = {
  node_id: string;
  path: string;
  content: string;
  sha256: string;
  summary: string;
  status: "proposal" | "waiting_permission" | "applied" | "failed";
  task_id: string | null;
};

export type CodeProposalApplication = {
  task_id: string;
  tool_call_id: string;
  approval_id: string;
};

export type GoalCodeProposalReview = {
  proposal: GoalCodeProposal;
  prepareApproval: () => Promise<CodeProposalApplication>;
};

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("La proposition de code reçue est invalide.");
  }
  return value as Record<string, unknown>;
}

function isIdentifier(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,200}$/.test(value);
}

function isBoundedCode(value: unknown): value is string {
  if (typeof value !== "string" || !value.length || value.length > 64_000) return false;
  let bytes = 0;
  for (const character of value) {
    const point = character.codePointAt(0) as number;
    if (point >= 0xd800 && point <= 0xdfff) return false;
    bytes += point <= 0x7f ? 1 : point <= 0x7ff ? 2 : point <= 0xffff ? 3 : 4;
    if (bytes > 64_000) return false;
  }
  return true;
}

export function parseGoalCodeProposal(
  value: unknown,
  goalId: string,
  nodeId: string,
): GoalCodeProposal {
  const item = record(value);
  if (
    !isIdentifier(goalId) || !isIdentifier(nodeId)
    || item.node_id !== nodeId
    || item.path !== `generated/${goalId}/${nodeId}/app.py`
    || !isBoundedCode(item.content)
    || typeof item.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(item.sha256)
    || typeof item.summary !== "string" || !item.summary.trim() || item.summary.length > 500
    || !["proposal", "waiting_permission", "applied", "failed"].includes(String(item.status))
    || (item.task_id !== null && !isIdentifier(item.task_id))
    || (["waiting_permission", "applied"].includes(String(item.status)) && item.task_id === null)
  ) {
    throw new Error("La proposition de code ne correspond pas à ce nœud ou dépasse les limites de revue.");
  }
  return {
    node_id: nodeId,
    path: item.path,
    content: item.content,
    sha256: item.sha256,
    summary: item.summary,
    status: item.status as GoalCodeProposal["status"],
    task_id: item.task_id as string | null,
  };
}

export function parseCodeProposalApplication(value: unknown): CodeProposalApplication {
  const item = record(value);
  if (!isIdentifier(item.task_id) || !isIdentifier(item.tool_call_id) || !isIdentifier(item.approval_id)) {
    throw new Error("La réponse ne contient pas la demande d’autorisation attendue. Actualisez la proposition.");
  }
  return { task_id: item.task_id, tool_call_id: item.tool_call_id, approval_id: item.approval_id };
}
