import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";
import { useRouter } from "expo-router";

import { ScreenShell } from "@/components/screen-shell";
import {
  ActionButton,
  Card,
  COLORS,
  EmptyState,
  ErrorBanner,
  SectionTitle,
  timeAgo,
} from "@/components/swarm-ui";
import {
  bootstrapSync,
  createGoal,
  getServerUrl,
  type Agent,
  type Bootstrap,
  type GoalAutonomyProfile,
  type GoalRecord,
  type GoalResult,
  type PlanNode,
} from "@/lib/api/client";
import {
  localSwarmSnapshot,
  type LocalSwarmSnapshot,
} from "@/lib/state/replica";
import { useLiveRefresh } from "@/lib/sync/live-sync-context";

type SwarmData = {
  agents: Agent[];
  goals: GoalRecord[];
  nodes: PlanNode[];
  results: GoalResult[];
};

type SwarmSource = "authoritative" | "cache" | null;

type SwarmLoadResult = {
  data: SwarmData;
  error: string | null;
  source: SwarmSource;
};

const EMPTY_DATA: SwarmData = { agents: [], goals: [], nodes: [], results: [] };
const PROFILES: { key: GoalAutonomyProfile; label: string }[] = [
  { key: "manual", label: "Manuel" },
  { key: "assisted", label: "Assisté" },
  { key: "autonomous", label: "Autonome" },
];

const GOAL_COLORS: Record<GoalRecord["status"], string> = {
  planning: COLORS.info,
  running: COLORS.info,
  waiting_permission: COLORS.warning,
  completed: COLORS.accent,
  failed: COLORS.danger,
  cancelled: COLORS.subtle,
  budget_exhausted: COLORS.warning,
};

const GOAL_LABELS: Record<GoalRecord["status"], string> = {
  planning: "Planification",
  running: "En cours",
  waiting_permission: "Permission",
  completed: "Terminé",
  failed: "Échoué",
  cancelled: "Annulé",
  budget_exhausted: "Budget épuisé",
};

function bootstrapData(bootstrap: Bootstrap): SwarmData {
  return {
    agents: bootstrap.agents,
    goals: bootstrap.goals ?? [],
    nodes: bootstrap.plan_nodes ?? [],
    results: bootstrap.goal_results ?? [],
  };
}

function cachedData(snapshot: LocalSwarmSnapshot): SwarmData {
  return {
    agents: snapshot.agents,
    goals: snapshot.goals,
    nodes: snapshot.plan_nodes,
    results: snapshot.goal_results,
  };
}

function emptyLoad(message: string): SwarmLoadResult {
  return { data: EMPTY_DATA, error: message, source: null };
}

async function cachedLoad(
  message: string,
  isCurrent: () => boolean,
): Promise<SwarmLoadResult | null> {
  const scope = await getServerUrl();
  const cached = await localSwarmSnapshot(scope);
  const currentScope = await getServerUrl();
  if (!isCurrent() || currentScope !== scope) return null;
  if (!cached || cached.origin !== scope) return emptyLoad(message);
  return {
    data: cachedData(cached),
    error: `Hors ligne — copie locale en lecture seule. ${message}`,
    source: "cache",
  };
}

async function loadSwarm(
  isCurrent: () => boolean,
): Promise<SwarmLoadResult | null> {
  try {
    const bootstrap = await bootstrapSync(isCurrent);
    if (!isCurrent()) return null;
    return { data: bootstrapData(bootstrap), error: null, source: "authoritative" };
  } catch (cause) {
    if (!isCurrent()) return null;
    const message = messageFor(cause);
    try {
      return await cachedLoad(message, isCurrent);
    } catch {
      return isCurrent() ? emptyLoad(message) : null;
    }
  }
}

function useSwarmData() {
  const refreshEpoch = useRef(0);
  const [data, setData] = useState<SwarmData>(EMPTY_DATA);
  const [source, setSource] = useState<SwarmSource>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const epoch = ++refreshEpoch.current;
    const isCurrent = () => refreshEpoch.current === epoch;
    setRefreshing(true);
    setError(null);
    try {
      const loaded = await loadSwarm(isCurrent);
      if (!loaded || !isCurrent()) return;
      setData(loaded.data);
      setSource(loaded.source);
      setError(loaded.error);
    } finally {
      if (isCurrent()) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void (async () => {
      await refresh();
    })();
    return () => {
      refreshEpoch.current += 1;
    };
  }, [refresh]);
  useLiveRefresh(refresh);

  return { data, error, refresh, refreshing, setError, source };
}

function goalNodes(nodes: PlanNode[], goalId: string) {
  return nodes.filter((node) => node.goal_run_id === goalId);
}

function GoalBadge({ goal }: { goal: GoalRecord }) {
  const color = GOAL_COLORS[goal.status];
  return (
    <View
      accessibilityLabel={`Statut: ${GOAL_LABELS[goal.status]}`}
      style={{
        alignSelf: "flex-start",
        backgroundColor: `${color}1f`,
        borderColor: `${color}66`,
        borderRadius: 999,
        borderWidth: 1,
        paddingHorizontal: 10,
        paddingVertical: 5,
      }}
    >
      <Text style={{ color, fontSize: 12, fontWeight: "700" }}>
        {GOAL_LABELS[goal.status]}
      </Text>
    </View>
  );
}

function GoalCard({
  goal,
  nodes,
  result,
  onOpen,
}: {
  goal: GoalRecord;
  nodes: PlanNode[];
  result: GoalResult | undefined;
  onOpen: () => void;
}) {
  const completed = nodes.filter((node) => node.status === "completed").length;
  const blocked = nodes.filter((node) => ["blocked", "failed"].includes(node.status)).length;
  const runningAgents = new Set(
    nodes
      .filter((node) => ["dispatched", "running", "waiting_permission", "waiting_capability"].includes(node.status))
      .map((node) => node.assigned_agent_id)
      .filter((agentId): agentId is string => Boolean(agentId)),
  );
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Ouvrir le but ${goal.objective}`}
      onPress={onOpen}
      style={({ pressed }) => ({ opacity: pressed ? 0.75 : 1 })}
      testID={`goal-row-${goal.id}`}
    >
      <Card>
        <View style={{ alignItems: "center", flexDirection: "row", justifyContent: "space-between" }}>
          <GoalBadge goal={goal} />
          <Text style={{ color: COLORS.subtle, fontSize: 11 }}>{timeAgo(goal.updated_at)}</Text>
        </View>
        <Text style={{ color: COLORS.text, fontSize: 17, fontWeight: "700", lineHeight: 23 }}>
          {goal.objective}
        </Text>
        <Text style={{ color: COLORS.muted }}>
          Phase {goal.current_phase} · {completed}/{nodes.length} étapes terminées
        </Text>
        <Text style={{ color: blocked ? COLORS.warning : COLORS.subtle, fontSize: 12 }}>
          {runningAgents.size} agent{runningAgents.size === 1 ? "" : "s"} actif{runningAgents.size === 1 ? "" : "s"} · {blocked} bloquée{blocked === 1 ? "" : "s"}
        </Text>
        {goal.evaluator_summary ? (
          <Text numberOfLines={2} style={{ color: COLORS.muted, lineHeight: 19 }}>
            Évaluation : {goal.evaluator_summary}
          </Text>
        ) : null}
        {result?.answer ? (
          <Text numberOfLines={2} style={{ color: COLORS.accent, lineHeight: 19 }}>
            Résultat : {result.answer}
          </Text>
        ) : null}
      </Card>
    </Pressable>
  );
}

function messageFor(cause: unknown) {
  return cause instanceof Error ? cause.message : String(cause);
}

function useGoalCreation(
  source: SwarmSource,
  refreshing: boolean,
  setError: (message: string | null) => void,
) {
  const router = useRouter();
  const [objective, setObjective] = useState("");
  const [profile, setProfile] = useState<GoalAutonomyProfile>("assisted");
  const [creating, setCreating] = useState(false);

  const submit = useCallback(async () => {
    const trimmedObjective = objective.trim();
    if (!trimmedObjective || source !== "authoritative" || creating || refreshing) return;
    setCreating(true);
    setError(null);
    try {
      const detail = await createGoal({
        autonomy_profile: profile,
        objective: trimmedObjective,
      });
      setObjective("");
      router.push({ pathname: "/goal/[id]", params: { id: detail.goal.id } });
    } catch (cause) {
      setError(`${messageFor(cause)} La création incertaine n’est pas renvoyée automatiquement.`);
    } finally {
      setCreating(false);
    }
  }, [creating, objective, profile, refreshing, router, setError, source]);

  return { creating, objective, profile, router, setObjective, setProfile, submit };
}

function OfflineNotice({ source }: { source: SwarmSource }) {
  if (source !== "cache") return null;
  return (
    <Text accessibilityLiveRegion="polite" style={{ color: COLORS.warning, lineHeight: 19 }}>
      Les données peuvent être périmées. Aucune création, annulation, relance ou permission n’est
      autorisée depuis cette copie.
    </Text>
  );
}

function GoalEmptyState({
  error,
  goalCount,
  refreshing,
}: {
  error: string | null;
  goalCount: number;
  refreshing: boolean;
}) {
  if (goalCount || refreshing || error) return null;
  return (
    <EmptyState
      title="Aucun but"
      subtitle="Crée un but vérifiable. Son démarrage restera une action séparée."
    />
  );
}

export default function SwarmScreen() {
  const { data, error, refresh, refreshing, setError, source } = useSwarmData();
  const creation = useGoalCreation(source, refreshing, setError);

  const agentNames = useMemo(
    () => new Map(data.agents.map((agent) => [agent.id, agent.name])),
    [data.agents],
  );
  const activeAgentIds = useMemo(() => new Set(
    data.nodes
      .filter((node) => ["dispatched", "running", "waiting_permission", "waiting_capability"].includes(node.status))
      .map((node) => node.assigned_agent_id)
      .filter((agentId): agentId is string => Boolean(agentId)),
  ), [data.nodes]);

  return (
    <ScreenShell
      title="Swarm"
      subtitle="Définis un but, puis suis la planification et les preuves du control plane Ubuntu."
      onRefresh={() => void refresh()}
      refreshing={refreshing}
      testID="swarm-screen"
    >
      <ErrorBanner message={error} />
      <OfflineNotice source={source} />

      <SectionTitle title="Nouveau but" />
      <Card>
        <Text style={{ color: COLORS.muted, lineHeight: 20 }}>
          Créer un but ne lance rien automatiquement. Le control plane reste autoritaire.
        </Text>
        <TextInput
          accessibilityLabel="Objectif du but"
          editable={source === "authoritative" && !creation.creating && !refreshing}
          multiline
          onChangeText={creation.setObjective}
          placeholder="Quel résultat vérifiable veux-tu obtenir?"
          placeholderTextColor={COLORS.subtle}
          style={{
            backgroundColor: COLORS.background,
            borderColor: COLORS.border,
            borderRadius: 12,
            borderWidth: 1,
            color: COLORS.text,
            minHeight: 96,
            padding: 12,
            textAlignVertical: "top",
          }}
          testID="goal-objective-input"
          value={creation.objective}
        />
        <View accessibilityLabel="Profil d’autonomie" style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
          {PROFILES.map((item) => {
            const selected = creation.profile === item.key;
            return (
              <Pressable
                accessibilityRole="button"
                accessibilityState={{ disabled: source !== "authoritative" || refreshing, selected }}
                disabled={source !== "authoritative" || creation.creating || refreshing}
                key={item.key}
                onPress={() => creation.setProfile(item.key)}
                style={{
                  backgroundColor: selected ? `${COLORS.accent}1f` : COLORS.panelRaised,
                  borderColor: selected ? COLORS.accent : COLORS.border,
                  borderRadius: 999,
                  borderWidth: 1,
                  minHeight: 44,
                  justifyContent: "center",
                  paddingHorizontal: 14,
                }}
              >
                <Text style={{ color: selected ? COLORS.accent : COLORS.muted, fontWeight: "700" }}>
                  {item.label}
                </Text>
              </Pressable>
            );
          })}
        </View>
        <ActionButton
          busy={creation.creating}
          disabled={source !== "authoritative" || refreshing || !creation.objective.trim()}
          label="Créer le but"
          onPress={() => void creation.submit()}
          testID="create-goal-button"
          variant="accent"
        />
      </Card>

      <SectionTitle title="Swarm actif" />
      <Card>
        <Text style={{ color: COLORS.text, fontSize: 18, fontWeight: "700" }}>
          {activeAgentIds.size} agent{activeAgentIds.size === 1 ? "" : "s"} en travail
        </Text>
        <Text style={{ color: COLORS.muted, lineHeight: 20 }}>
          {activeAgentIds.size
            ? [...activeAgentIds].map((id) => agentNames.get(id) ?? id).join(", ")
            : "Aucun agent n’exécute actuellement un nœud."}
        </Text>
        <ActionButton
          label="Voir le registre des agents"
          onPress={() => creation.router.push("/agents")}
        />
        <ActionButton
          label="Voir les accords"
          onPress={() => creation.router.push("/approvals")}
        />
      </Card>

      <SectionTitle title="Buts" />
      <GoalEmptyState error={error} goalCount={data.goals.length} refreshing={refreshing} />
      <View style={{ gap: 12 }} testID="goal-list">
        {data.goals.map((goal) => (
          <GoalCard
            goal={goal}
            key={goal.id}
            nodes={goalNodes(data.nodes, goal.id)}
            onOpen={() =>
              creation.router.push({ pathname: "/goal/[id]", params: { id: goal.id } })
            }
            result={data.results.find((result) => result.goal_run_id === goal.id)}
          />
        ))}
      </View>
    </ScreenShell>
  );
}
