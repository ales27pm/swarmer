import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pressable, ScrollView, Text, View } from "react-native";
import { useRouter } from "expo-router";

import { KeyboardInputGroup, KeyboardTextInput, ScreenShell } from "@/components/screen-shell";
import { ActionButton, Card, COLORS, EmptyState, ErrorBanner } from "@/components/swarm-ui";
import { getActivityCatalog } from "@/lib/api/client";
import { catalogSearchText, type ActivityCatalog, type CatalogAvailability, type CatalogSkill } from "@/lib/api/activity-catalog";
import { useLiveRefresh } from "@/lib/sync/live-sync-context";

const AVAILABILITY: Record<CatalogAvailability, { label: string; color: string }> = {
  goal_ready: { label: "Disponible pour un but", color: COLORS.accent },
  parameters_required: { label: "Paramètres requis", color: COLORS.info },
  worker_unavailable: { label: "Agent indisponible", color: COLORS.warning },
  iphone_request: { label: "Accès iPhone intégré", color: COLORS.info },
  planned: { label: "À intégrer", color: COLORS.muted },
  policy_denied: { label: "Désactivé dans les réglages", color: COLORS.warning },
  unknown: { label: "Disponibilité à vérifier", color: COLORS.warning },
};
type Filter = "all" | "implemented" | "ready";
const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "Tout le catalogue" },
  { id: "implemented", label: "Outils intégrés" },
  { id: "ready", label: "Disponibles pour un but" },
];

function Choice({ label, selected, onPress }: { label: string; selected: boolean; onPress: () => void }) {
  return (
    <Pressable accessibilityRole="button" accessibilityState={{ selected }} onPress={onPress}
      style={{ borderRadius: 20, paddingHorizontal: 14, paddingVertical: 12, backgroundColor: selected ? COLORS.accent : COLORS.panelRaised }}>
      <Text style={{ color: selected ? COLORS.accentText : COLORS.text, fontWeight: "600" }}>{label}</Text>
    </Pressable>
  );
}

function SkillDetails({ skill, fresh }: { skill: CatalogSkill; fresh: boolean }) {
  const status = AVAILABILITY[fresh || skill.execution.kind === "planned" ? skill.availability.state : "unknown"];
  return (
    <View style={{ gap: 8, paddingTop: 14, borderTopColor: COLORS.border, borderTopWidth: 1 }} testID={`catalog-skill-${skill.id}`}>
      <Text style={{ color: COLORS.text, fontSize: 17, fontWeight: "700" }}>{skill.title}</Text>
      <Text style={{ color: status.color, fontWeight: "600" }}>{status.label}</Text>
      <Text style={{ color: COLORS.muted, lineHeight: 21 }}>{skill.description}</Text>
      <Text style={{ color: COLORS.text, lineHeight: 21 }}>Résultat : {skill.output}</Text>
      {skill.inputs.length ? <Text style={{ color: COLORS.muted, lineHeight: 21 }}>À fournir : {skill.inputs.join(" · ")}</Text> : null}
      <Text style={{ color: COLORS.muted, lineHeight: 21 }}>
        {fresh || skill.execution.kind === "planned" ? skill.availability.reason : "Actualisez le catalogue pour vérifier l’état des outils."}
      </Text>
      {skill.requirements.length ? <Text style={{ color: COLORS.muted, lineHeight: 21 }}>Prérequis : {skill.requirements.join(" · ")}</Text> : null}
    </View>
  );
}

export default function CatalogScreen() {
  const router = useRouter();
  const [catalog, setCatalog] = useState<ActivityCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [receivedAt, setReceivedAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [query, setQuery] = useState("");
  const [domain, setDomain] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [expanded, setExpanded] = useState<string | null>(null);
  const requestId = useRef(0);

  const refresh = useCallback(async () => {
    const id = ++requestId.current;
    setRefreshing(true);
    setError(null);
    setReceivedAt(0);
    try {
      const value = await getActivityCatalog();
      if (id !== requestId.current) return;
      setCatalog(value);
      setReceivedAt(Date.now());
      setNow(Date.now());
    } catch (cause) {
      if (id === requestId.current) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (id === requestId.current) setRefreshing(false);
    }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => { requestId.current += 1; clearInterval(timer); };
  }, [refresh]);
  useLiveRefresh(refresh);

  const observedAt = catalog ? Date.parse(catalog.generated_at) : 0;
  const fresh = receivedAt > 0 && now - receivedAt < 90_000
    && observedAt <= now + 5_000 && now - observedAt < 90_000 && !error;
  const skills = useMemo(() => new Map(catalog?.skills.map((skill) => [skill.id, skill])), [catalog]);
  const domains = useMemo(() => new Map(catalog?.domains.map((item) => [item.id, item])), [catalog]);
  const needle = catalogSearchText(query.trim());
  const filtered = useMemo(() => catalog?.roles.flatMap((role) => {
    if (domain && role.domain_id !== domain) return [];
    const roleSkills = role.skill_ids.map((id) => skills.get(id)!).filter((skill) =>
      filter === "all" || (filter === "implemented" ? skill.execution.kind !== "planned" : fresh && skill.availability.state === "goal_ready")
    );
    if (!roleSkills.length) return [];
    const searchable = [role.title, role.description, domains.get(role.domain_id)?.title ?? "", ...role.examples,
      ...roleSkills.flatMap((skill) => [skill.title, skill.description, skill.output, ...skill.inputs, ...skill.requirements])].join(" ");
    return needle && !catalogSearchText(searchable).includes(needle) ? [] : [{ role, roleSkills }];
  }) ?? [], [catalog, domain, domains, filter, fresh, needle, skills]);
  const integrated = catalog?.skills.filter((skill) => skill.execution.kind !== "planned").length ?? 0;

  return (
    <ScreenShell title="Catalogue d’agents" subtitle="Des profils pour le code, le travail et le quotidien. Chaque compétence précise ses outils et ses prérequis."
      onRefresh={() => void refresh()} refreshing={refreshing} testID="catalog-screen">
      <ErrorBanner message={error} />
      <ActionButton label="Actualiser le catalogue" busy={refreshing} onPress={() => void refresh()} />
      <ActionButton label="Voir les agents connectés" onPress={() => router.push("/agents")} />
      {!catalog && refreshing ? <Text style={{ color: COLORS.muted }}>Chargement du catalogue…</Text> : null}
      {catalog ? <>
        <Card>
          <Text style={{ color: COLORS.text, fontSize: 20, fontWeight: "700" }}>
            {catalog.domains.length} domaines · {catalog.roles.length} profils
          </Text>
          <Text style={{ color: COLORS.muted, lineHeight: 21 }}>
            {catalog.skills.length} compétences · {integrated} outils intégrés · {catalog.skills.length - integrated} à intégrer
          </Text>
          <Text style={{ color: COLORS.muted, lineHeight: 21 }}>
            Un profil décrit un rôle. Ses compétences peuvent utiliser plusieurs agents; leur disponibilité dépend des connexions et des réglages.
          </Text>
          <Text style={{ color: fresh ? COLORS.subtle : COLORS.warning, lineHeight: 20 }}>
            {fresh ? "Disponibilité au dernier relevé. Le serveur la vérifie à nouveau au démarrage d’un but." : "Disponibilité à actualiser. Les fiches du catalogue restent consultables."}
          </Text>
        </Card>
        <KeyboardInputGroup dismissKeyboard>
          <KeyboardTextInput accessibilityLabel="Rechercher une activité" placeholder="CRM, agenda, Python, budget…" placeholderTextColor={COLORS.subtle}
            value={query} onChangeText={setQuery} autoCorrect={false} maxLength={200}
            style={{ backgroundColor: COLORS.panel, color: COLORS.text, borderColor: COLORS.border, borderWidth: 1, borderRadius: 12, padding: 14, fontSize: 16 }} />
        </KeyboardInputGroup>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8 }} accessibilityLabel="Domaines du catalogue">
          <Choice label="Tous les domaines" selected={domain === null} onPress={() => setDomain(null)} />
          {catalog.domains.map((item) => <Choice key={item.id} label={item.title} selected={domain === item.id} onPress={() => setDomain(item.id)} />)}
        </ScrollView>
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
          {FILTERS.map((item) => <Choice key={item.id} label={item.label} selected={filter === item.id} onPress={() => setFilter(item.id)} />)}
        </View>
        <Text accessibilityLiveRegion="polite" style={{ color: COLORS.muted }}>{filtered.length} profil{filtered.length === 1 ? "" : "s"} affiché{filtered.length === 1 ? "" : "s"}</Text>
        {!filtered.length ? <EmptyState title="Aucun profil pour ces critères" subtitle="Essayez un autre mot ou élargissez les filtres." /> : null}
        {filtered.map(({ role, roleSkills }) => (
          <Card key={role.id} testID={`catalog-role-${role.id}`}>
            <Text style={{ color: COLORS.accent, fontSize: 12, fontWeight: "700" }}>{domains.get(role.domain_id)?.title}</Text>
            <Text accessibilityRole="header" style={{ color: COLORS.text, fontSize: 20, fontWeight: "700" }}>{role.title}</Text>
            <Text style={{ color: COLORS.muted, lineHeight: 21 }}>{role.description}</Text>
            <Text style={{ color: COLORS.muted, lineHeight: 20 }}>
              {roleSkills.map((skill) => skill.title).join(" · ")}
            </Text>
            <ActionButton label={expanded === role.id ? "Réduire la fiche" : "Voir les compétences"}
              accessibilityLabel={`${expanded === role.id ? "Réduire" : "Ouvrir"} la fiche ${role.title}`}
              onPress={() => setExpanded((value) => value === role.id ? null : role.id)} />
            {expanded === role.id ? <>
              {role.examples.length ? <View style={{ gap: 6 }}>
                <Text style={{ color: COLORS.text, fontWeight: "700" }}>Exemples de demandes</Text>
                {role.examples.map((example) => <Text selectable key={example} style={{ color: COLORS.muted, lineHeight: 21 }}>• {example}</Text>)}
              </View> : null}
              {roleSkills.map((skill) => <SkillDetails key={skill.id} skill={skill} fresh={Boolean(fresh)} />)}
            </> : null}
          </Card>
        ))}
      </> : null}
    </ScreenShell>
  );
}
