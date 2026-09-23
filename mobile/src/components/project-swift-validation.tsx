import { useEffect, useRef, useState } from "react";
import { AppState, Text, TextInput, View } from "react-native";

import { ActionButton, COLORS, ErrorBanner } from "@/components/swarm-ui";
import { cancelSwiftProjectValidation, getSwiftProjectValidation, listAgents, type Agent } from "@/lib/application-api/server";
import { parseSwiftTarget, type ProjectReview, type SwiftValidation, type SwiftValidationAttempt } from "@/lib/api/project";
import { subscribeConnectionChanges } from "@/lib/connection-events";

const ACTIVE = new Set<SwiftValidation["status"]>(["queued", "assigned", "claimed", "running"]);
const LABELS: Record<SwiftValidation["status"], string> = {
  queued: "En file", assigned: "Attribuée", claimed: "Prise en charge", running: "En cours", quarantined: "Bloquée par la politique du serveur",
  passed: "Validation réussie", failed: "Validation échouée", cancelled: "Validation annulée", stale: "Validation périmée",
};
const fieldStyle = { color: COLORS.text, borderColor: COLORS.border, borderWidth: 1, padding: 10, borderRadius: 8 };

/** Explicit revision consent; a compiler receipt never promotes the project to completed. */
export function ProjectSwiftValidation({ goalId, review, disabled }: { goalId: string; review: ProjectReview; disabled: boolean }) {
  const project = review.project;
  const xcodeProjects = [...new Set(project.files.map(({ path }) => path.split("/")[0]).filter((path) => /\.(xcodeproj|xcworkspace)$/.test(path)))];
  const hasPackage = project.files.some(({ path }) => path === "Package.swift");
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentId, setAgentId] = useState("");
  const [kind, setKind] = useState<"swiftpm" | "xcode">(hasPackage ? "swiftpm" : "xcode");
  const [xcodeProject, setXcodeProject] = useState("");
  const [scheme, setScheme] = useState("");
  const [destination, setDestination] = useState("");
  const [operation, setOperation] = useState<"build" | "test">("test");
  const [consent, setConsent] = useState(false);
  const [validation, setValidation] = useState<SwiftValidation | null>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState<SwiftValidationAttempt | null>(null);
  const epoch = useRef(0);
  const inFlight = useRef(false);
  const paired = useRef(true);
  const active = Boolean(validation && ACTIVE.has(validation.status));
  const locked = disabled || busy || active || Boolean(attempt);
  const workers = agents.filter((agent) => agent.status === "online" && agent.skills.includes(`code.swift.${operation}`));
  const eligible = workers.some((agent) => agent.id === agentId);
  const targetReady = kind === "swiftpm" ? hasPackage : xcodeProjects.includes(xcodeProject)
    && /^[A-Za-z0-9_][A-Za-z0-9_ .-]{0,99}$/.test(scheme) && /^[A-Za-z0-9_-]{1,64}$/.test(destination);

  useEffect(() => {
    let disposed = false;
    let reading = false;
    paired.current = true;
    const refresh = async () => {
      if (disposed || reading || !paired.current || disabled || inFlight.current || AppState.currentState !== "active") return;
      reading = true;
      const version = epoch.current;
      try {
        const [value, available] = await Promise.all([getSwiftProjectValidation(goalId), listAgents()]);
        if (!disposed && version === epoch.current) { setValidation(value); setAgents(available); setReady(true); }
      } catch (cause) {
        if (!disposed && version === epoch.current) { setReady(false); setError(cause instanceof Error ? cause.message : String(cause)); }
      } finally { reading = false; }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    const state = AppState.addEventListener("change", (value) => { if (value === "active") void refresh(); });
    const unsubscribe = subscribeConnectionChanges(() => {
      paired.current = false; epoch.current += 1; setReady(false); setConsent(false); setAttempt(null); setValidation(null);
      setError("Le jumelage a changé. Rechargez la révision avant de poursuivre.");
    });
    return () => { disposed = true; epoch.current += 1; clearInterval(timer); state.remove(); unsubscribe(); };
  }, [goalId, disabled]);

  const run = async (action: () => Promise<SwiftValidation>, submitting: boolean) => {
    if (disabled || inFlight.current || !paired.current) return;
    const version = ++epoch.current;
    inFlight.current = true; setBusy(true); setError(null);
    try {
      const value = await action();
      if (version === epoch.current) { setValidation(value); setUncertain(false); setReady(true); }
    } catch (cause) {
      if (version === epoch.current) {
        if (submitting) setUncertain(true);
        setError(`${cause instanceof Error ? cause.message : String(cause)} ${submitting ? "La demande peut avoir été créée. Réessayez la même demande ou attendez son statut." : "Le statut sera actualisé automatiquement."}`);
      }
    } finally { inFlight.current = false; if (version === epoch.current) setBusy(false); }
  };
  const start = () => {
    if (!ready || locked || !consent || !eligible || !targetReady) return;
    try {
      const prepared = review.prepareSwiftValidation({ agentId, operation, target: parseSwiftTarget(kind === "swiftpm"
        ? { kind } : { kind, project: xcodeProject, scheme, destination }) });
      setAttempt(prepared);
      void run(prepared.send, true);
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  };

  return <View style={{ gap: 10 }} testID="project-swift-validation">
    <Text style={{ color: COLORS.text, fontWeight: "800" }}>Validation native sur l’iMac</Text>
    <Text style={{ color: COLORS.muted }}>La compilation et les tests exécutent le code de cette révision sur le Mac sélectionné. Relisez les fichiers avant d’autoriser cette exécution.</Text>
    <Text selectable style={{ color: COLORS.subtle }}>Révision {project.revision_id} · {project.sha256}</Text>
    <ActionButton label={operation === "test" ? "✓ Compiler et tester" : "Compiler et tester"} disabled={locked} onPress={() => { setOperation("test"); setConsent(false); }} />
    <ActionButton label={operation === "build" ? "✓ Compiler seulement" : "Compiler seulement"} disabled={locked} onPress={() => { setOperation("build"); setConsent(false); }} />
    <Text style={{ color: COLORS.muted }}>Worker de compilation</Text>
    {workers.map((agent) => <ActionButton key={agent.id} label={`${agentId === agent.id ? "✓ " : ""}${agent.name} · ${agent.id}`} disabled={locked} onPress={() => { setAgentId(agent.id); setConsent(false); }} />)}
    {ready && !workers.length ? <Text style={{ color: COLORS.warning }}>Aucun worker Swift compatible n’est actuellement en ligne.</Text> : null}
    {hasPackage ? <ActionButton label={kind === "swiftpm" ? "✓ Package.swift" : "Package.swift"} disabled={locked} onPress={() => { setKind("swiftpm"); setConsent(false); }} /> : null}
    {xcodeProjects.map((name) => <ActionButton key={name} label={`${kind === "xcode" && xcodeProject === name ? "✓ " : ""}${name}`} disabled={locked} onPress={() => { setKind("xcode"); setXcodeProject(name); setConsent(false); }} />)}
    {kind === "xcode" ? <>
      <Text style={{ color: COLORS.muted }}>Schéma Xcode et nom de destination configurée sur le worker</Text>
      <TextInput accessibilityLabel="Schéma Xcode" value={scheme} editable={!locked} autoCapitalize="none" autoCorrect={false} maxLength={100} style={fieldStyle} onChangeText={(value) => { setScheme(value); setConsent(false); }} />
      <TextInput accessibilityLabel="Destination autorisée du worker" value={destination} editable={!locked} autoCapitalize="none" autoCorrect={false} maxLength={64} style={fieldStyle} onChangeText={(value) => { setDestination(value); setConsent(false); }} />
    </> : null}
    {!hasPackage && !xcodeProjects.length ? <Text style={{ color: COLORS.warning }}>Cette révision doit contenir un Package.swift ou un projet Xcode à sa racine pour être compilée par ce parcours.</Text> : null}
    <ActionButton label={consent ? "Exécution de cette révision autorisée" : "J’autorise l’exécution de cette révision sur ce Mac"} disabled={locked || !ready || !eligible || !targetReady} onPress={() => setConsent(!consent)} />
    <ActionButton label={operation === "test" ? "Compiler et tester cette révision" : "Compiler cette révision"} variant="accent" disabled={!ready || locked || !consent || !eligible || !targetReady} busy={busy} onPress={start} />
    {uncertain && attempt ? <ActionButton label="Réessayer la même demande" disabled={disabled || busy || active} onPress={() => void run(attempt.send, true)} /> : null}
    <ErrorBanner message={error} />
    {validation ? <>
      <Text style={{ color: validation.status === "passed" ? COLORS.accent : COLORS.warning }}>{LABELS[validation.status]} · {validation.operation === "test" ? "compilation et tests" : "compilation seule"}</Text>
      <Text selectable style={{ color: COLORS.subtle }}>Révision {validation.revision_id} · worker {validation.agent_id}</Text>
      {validation.revision_id !== project.revision_id || validation.sha256 !== project.sha256 ? <Text style={{ color: COLORS.warning }}>Ce résultat concerne une autre révision ; il ne valide pas les fichiers affichés.</Text> : null}
      {validation.receipt ? <Text style={{ color: COLORS.muted }}>{validation.receipt.tests_executed} tests exécutés · {validation.receipt.test_failures} échecs · {validation.receipt.duration_ms} ms</Text> : null}
      {active ? <ActionButton label="Annuler la validation native" disabled={disabled || busy} onPress={() => void run(() => cancelSwiftProjectValidation(goalId, validation.validation_id), false)} /> : null}
      {!active && !uncertain && attempt ? <ActionButton label="Préparer une nouvelle validation" disabled={disabled || busy} onPress={() => { setAttempt(null); setConsent(false); }} /> : null}
    </> : null}
    <Text style={{ color: COLORS.subtle }}>Un reçu de compilation ne termine pas automatiquement le projet et ne constitue pas un test sur iPhone.</Text>
  </View>;
}
