import { useCallback, useEffect, useRef, useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";
import { useLocalSearchParams, useRouter } from "expo-router";

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
  ApiError,
  ConnectionChangedError,
  bootstrapSync,
  createGoal,
  getServerUrl,
  type Agent,
  type Bootstrap,
  type GoalAutonomyProfile,
  type GoalRecord,
  type GoalResult,
  type PlanNode,
} from "@/lib/application-api/server";
import { subscribeConnectionChanges } from "@/lib/connection-events";
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
  scope: string | null;
};

const EMPTY_DATA: SwarmData = { agents: [], goals: [], nodes: [], results: [] };
const CONNECTION_CHANGED = "Le jumelage a changé. Actualise les projets depuis cette connexion.";
const PROFILES: { key: GoalAutonomyProfile; label: string; description: string }[] = [
  { key: "manual", label: "Manuel", description: "Tu pilotes les étapes du projet." },
  { key: "assisted", label: "Assisté", description: "L’équipe t’accompagne dans l’avancement du projet." },
  { key: "autonomous", label: "Autonome", description: "L’équipe avance dans les limites du projet et te sollicite pour les autorisations nécessaires." },
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
  waiting_permission: "Autorisation",
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
  return { data: EMPTY_DATA, error: message, source: null, scope: null };
}

async function cachedLoad(
  message: string,
  isCurrent: () => boolean,
  scope: string,
): Promise<SwarmLoadResult | null> {
  if (await getServerUrl() !== scope) return emptyLoad(CONNECTION_CHANGED);
  if (!isCurrent()) return null;
  const cached = await localSwarmSnapshot(scope);
  const currentScope = await getServerUrl();
  if (!isCurrent()) return null;
  if (currentScope !== scope) return emptyLoad(CONNECTION_CHANGED);
  if (!cached || cached.origin !== scope) return emptyLoad(message);
  return {
    data: cachedData(cached),
    error: `Hors ligne — copie locale en lecture seule. ${message}`,
    source: "cache",
    scope,
  };
}

async function loadSwarm(
  isCurrent: () => boolean,
  onScope: (scope: string) => void,
): Promise<SwarmLoadResult | null> {
  let scope: string | null = null;
  try {
    scope = await getServerUrl();
    if (!isCurrent()) return null;
    onScope(scope);
    const bootstrap = await bootstrapSync(isCurrent);
    if (!isCurrent()) return null;
    if (await getServerUrl() !== scope) return emptyLoad(CONNECTION_CHANGED);
    return { data: bootstrapData(bootstrap), error: null, source: "authoritative", scope };
  } catch (cause) {
    if (!isCurrent()) return null;
    const message = messageFor(cause);
    if (!scope || cause instanceof ConnectionChangedError || (cause instanceof ApiError && [401, 403].includes(cause.status))) {
      return emptyLoad(message);
    }
    try {
      return await cachedLoad(message, isCurrent, scope);
    } catch {
      return isCurrent() ? emptyLoad(message) : null;
    }
  }
}

function useSwarmData() {
  const refreshEpoch = useRef(0);
  const dataScope = useRef<string | null>(null);
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
      const loaded = await loadSwarm(isCurrent, (scope) => {
        if (dataScope.current !== scope) {
          dataScope.current = null;
          setData(EMPTY_DATA);
          setSource(null);
        }
      });
      if (!loaded || !isCurrent()) return;
      dataScope.current = loaded.scope;
      setData(loaded.data);
      setSource(loaded.source);
      setError(loaded.error);
    } finally {
      if (isCurrent()) setRefreshing(false);
    }
  }, []);

  useEffect(() => subscribeConnectionChanges(() => {
    refreshEpoch.current += 1;
    dataScope.current = null;
    setData(EMPTY_DATA);
    setSource(null);
    setRefreshing(false);
    setError(CONNECTION_CHANGED);
  }), []);

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
      accessibilityLabel={`Ouvrir le projet ${goal.objective}`}
      onPress={onOpen}
      style={({ pressed }) => ({ opacity: pressed ? 0.75 : 1 })}
      testID={`goal-row-${goal.id}`}
    >
      <Card>
        <View style={{ alignItems: "center", flexDirection: "row", justifyContent: "space-between" }}>
          <GoalBadge goal={goal} />
          <Text style={{ color: COLORS.subtle, fontSize: 11 }}>{timeAgo(goal.updated_at)}</Text>
        </View>
        <Text numberOfLines={3} style={{ color: COLORS.text, fontSize: 17, fontWeight: "700", lineHeight: 23 }}>
          {goal.objective}
        </Text>
        {nodes.length ? (
          <View style={{ gap: 6 }}>
            <View accessibilityRole="progressbar" accessibilityLabel="Étapes terminées" accessibilityValue={{ min: 0, max: nodes.length, now: completed, text: `${completed} sur ${nodes.length} étapes terminées` }} style={{ backgroundColor: COLORS.panelRaised, borderRadius: 3, height: 6, overflow: "hidden" }}>
              <View style={{ backgroundColor: COLORS.accent, height: "100%", width: `${completed / nodes.length * 100}%` }} />
            </View>
            <Text style={{ color: COLORS.muted, fontSize: 12 }}>{completed}/{nodes.length} étapes terminées</Text>
          </View>
        ) : <Text style={{ color: COLORS.muted, fontSize: 12 }}>Aucune étape enregistrée</Text>}
        <Text style={{ color: blocked ? COLORS.warning : COLORS.subtle, fontSize: 12 }}>
          {runningAgents.size} agent{runningAgents.size === 1 ? "" : "s"} mobilisé{runningAgents.size === 1 ? "" : "s"} · {blocked} bloquée{blocked === 1 ? "" : "s"}
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
  const creationEpoch = useRef(0);
  const creatingRef = useRef(false);

  useEffect(() => {
    const unsubscribe = subscribeConnectionChanges(() => {
      creationEpoch.current += 1;
      if (creatingRef.current) {
        setError("Le jumelage a changé pendant la création. Vérifie le serveur précédent avant de réessayer.");
      }
      creatingRef.current = false;
      setCreating(false);
    });
    return () => { creationEpoch.current += 1; unsubscribe(); };
  }, [setError]);

  const submit = useCallback(async () => {
    const trimmedObjective = objective.trim();
    if (!trimmedObjective || source !== "authoritative" || creatingRef.current || refreshing) return;
    const epoch = ++creationEpoch.current;
    const isCurrent = () => epoch === creationEpoch.current;
    creatingRef.current = true;
    setCreating(true);
    setError(null);
    try {
      const detail = await createGoal({
        autonomy_profile: profile,
        objective: trimmedObjective,
      }, isCurrent);
      if (!isCurrent()) return;
      setObjective("");
      router.push({ pathname: "/goal/[id]", params: { id: detail.goal.id } });
    } catch (cause) {
      if (isCurrent()) setError(`${messageFor(cause)} La création incertaine n’est pas renvoyée automatiquement.`);
    } finally {
      if (isCurrent()) { creatingRef.current = false; setCreating(false); }
    }
  }, [objective, profile, refreshing, router, setError, source]);

  return { creating, objective, profile, router, setObjective, setProfile, submit };
}

function OfflineNotice({ source }: { source: SwarmSource }) {
  if (source !== "cache") return null;
  return (
    <Text accessibilityLiveRegion="polite" style={{ color: COLORS.warning, lineHeight: 19 }}>
      Les données peuvent être périmées. Les actions reprendront après
      la reconnexion au serveur.
    </Text>
  );
}

function GoalEmptyState({
  error,
  goalCount,
  refreshing,
  source,
}: {
  error: string | null;
  goalCount: number;
  refreshing: boolean;
  source: SwarmSource;
}) {
  if (goalCount) return null;
  if (refreshing) return <Text accessibilityLiveRegion="polite" style={{ color: COLORS.muted }}>Chargement des projets…</Text>;
  if (source === "cache") return <Text style={{ color: COLORS.muted }}>Aucun projet dans la copie locale. Reconnecte le serveur pour vérifier.</Text>;
  if (source !== "authoritative" || error) return null;
  return (
    <EmptyState
      title="Aucun projet"
      subtitle="Ajoute un projet pour organiser le travail de ton équipe."
    />
  );
}

export default function SwarmScreen() {
  const { data, error, refresh, refreshing, setError, source } = useSwarmData();
  const creation = useGoalCreation(source, refreshing, setError);
  const [creationOpen, setCreationOpen] = useState(false);

  const params = useLocalSearchParams<{ create?: string }>();
  useEffect(() => {
    if (params.create === "1") {
      setCreationOpen(true);
      creation.router.setParams?.({ create: undefined });
    }
  }, [params.create, creation.router]);

  return (
    <ScreenShell
      showTitle={false}
      title="Projets"
      subtitle="Un objectif, son contexte et ses résultats au même endroit."
      onRefresh={() => void refresh()}
      refreshing={refreshing}
      testID="swarm-screen"
    >
      <ErrorBanner message={error} />
      <OfflineNotice source={source} />
      {source !== "authoritative" ? <View style={{ gap: 8 }}>
        <Text style={{ color: COLORS.muted }}>{refreshing ? "Projets non vérifiés" : "Connecte le serveur pour vérifier les projets."}</Text>
        <ActionButton label="Vérifier la connexion dans Réglages" onPress={() => creation.router.push("/settings")} testID="swarm-open-settings" />
      </View> : null}
      {error ? <ActionButton label="Actualiser les projets" onPress={() => void refresh()} busy={refreshing} testID="swarm-retry" /> : null}
      {refreshing && data.goals.length ? <Text accessibilityLiveRegion="polite" style={{ color: COLORS.muted }}>Actualisation… Les projets affichés proviennent de la dernière lecture.</Text> : null}

      <View style={{ alignItems: "center", flexDirection: "row", flexWrap: "wrap", gap: 8, justifyContent: "space-between" }}>
        <SectionTitle title="Tes projets" />
        <Pressable
          accessibilityRole="button"
          accessibilityState={{ expanded: creationOpen }}
          onPress={() => setCreationOpen((expanded) => !expanded)}
          style={{ minHeight: 48, justifyContent: "center", paddingHorizontal: 4 }}
          testID="new-project-disclosure"
        >
          <Text style={{ color: COLORS.accent, fontSize: 14, fontWeight: "700" }}>
            {creationOpen ? "Fermer le formulaire" : "Nouveau projet"}
          </Text>
        </Pressable>
      </View>
      {creationOpen ? (
        <Card>
          <Text style={{ color: COLORS.muted, lineHeight: 20 }}>
            Décris le résultat souhaité. Le démarrage se fait ensuite, depuis le projet.
          </Text>
          <TextInput
            accessibilityLabel="Objectif du projet"
            editable={source === "authoritative" && !creation.creating && !refreshing}
            multiline
            onChangeText={creation.setObjective}
            placeholder="Qu’aimerais-tu accomplir?"
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
                  accessibilityState={{ disabled: source !== "authoritative" || creation.creating || refreshing, selected }}
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
          <Text style={{ color: COLORS.muted, fontSize: 13, lineHeight: 19 }}>
            {PROFILES.find((item) => item.key === creation.profile)?.description}
          </Text>
          <ActionButton
            busy={creation.creating}
            disabled={source !== "authoritative" || refreshing || !creation.objective.trim()}
            label="Créer le projet"
            onPress={() => void creation.submit()}
            testID="create-goal-button"
            variant="accent"
          />
        </Card>
      ) : null}
      <GoalEmptyState error={error} goalCount={data.goals.length} refreshing={refreshing} source={source} />
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
