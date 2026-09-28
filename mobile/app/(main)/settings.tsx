import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { Text, TextInput, View } from "react-native";
import { useRouter } from "expo-router";

import { ScreenShell } from "@/components/screen-shell";
import { SettingsDisclosure, SettingsGroup, SettingsNavigationRow } from "@/components/settings-section";
import { SemanticMemoryPanel } from "@/components/semantic-memory-panel";
import { ActionButton, COLORS, ErrorBanner, SectionTitle, timeAgo, useAccessibilityAnnouncement } from "@/components/swarm-ui";
import {
  bootstrapSync,
  getServerUrl,
  hasDeviceToken,
  listAudit,
  pairConnection,
  type AuditEvent,
  type Bootstrap,
} from "@/lib/application-api/server";

function ControlPlaneSection({
  activeUrl,
  onUrlChange,
  paired,
  url,
}: {
  activeUrl: string | null;
  onUrlChange: (value: string) => void;
  paired: boolean;
  url: string;
}) {
  const candidateUrl = url.trim().replace(/\/+$/, "");
  return (
    <>
      <Text style={{ color: COLORS.muted, fontSize: 13 }}>
          Utilise l’adresse HTTPS de ton serveur. Elle est enregistrée uniquement après un jumelage réussi.
      </Text>
      <Text style={{ color: COLORS.muted, fontSize: 12, fontWeight: "700" }}>
        Adresse du serveur
      </Text>
      <TextInput
        accessibilityLabel="Adresse du serveur"
        autoCapitalize="none"
        autoCorrect={false}
        onChangeText={onUrlChange}
        placeholder="https://mon-serveur.example"
        placeholderTextColor={COLORS.subtle}
        style={{ backgroundColor: COLORS.background, borderColor: COLORS.border, borderRadius: 12, borderWidth: 1, color: COLORS.text, minHeight: 46, paddingHorizontal: 12 }}
        testID="server-url-input"
        value={url}
      />
      {paired && activeUrl && candidateUrl !== activeUrl ? (
        <Text accessibilityRole="alert" style={{ color: COLORS.warning, lineHeight: 19 }}>
          Cette adresse est une candidate non vérifiée. La connexion active reste {activeUrl} jusqu’à un nouveau jumelage réussi.
        </Text>
      ) : null}
    </>
  );
}

function PairingSection({
  busy,
  code,
  deviceName,
  onCodeChange,
  onDeviceNameChange,
  onPair,
}: {
  busy: "pair" | null;
  code: string;
  deviceName: string;
  onCodeChange: (value: string) => void;
  onDeviceNameChange: (value: string) => void;
  onPair: () => void;
}) {
  return (
    <>
      <Text selectable style={{ color: COLORS.muted, fontSize: 13, lineHeight: 19 }}>
        Sur le serveur, génère un code temporaire à six chiffres avec la procédure de jumelage, puis saisis-le ici.
      </Text>
      <Text style={{ color: COLORS.muted, fontSize: 12, fontWeight: "700" }}>Nom de cet appareil</Text>
      <TextInput
        accessibilityLabel="Nom de cet appareil"
        onChangeText={onDeviceNameChange}
        placeholder="Nom de l’appareil"
        placeholderTextColor={COLORS.subtle}
        style={{ backgroundColor: COLORS.background, borderColor: COLORS.border, borderRadius: 12, borderWidth: 1, color: COLORS.text, minHeight: 46, paddingHorizontal: 12 }}
        testID="device-name-input"
        value={deviceName}
      />
      <Text style={{ color: COLORS.muted, fontSize: 12, fontWeight: "700" }}>Code de jumelage (6 chiffres)</Text>
      <TextInput
        accessibilityLabel="Code de jumelage à six chiffres"
        keyboardType="number-pad"
        maxLength={6}
        onChangeText={onCodeChange}
        placeholder="123456"
        placeholderTextColor={COLORS.subtle}
        style={{ backgroundColor: COLORS.background, borderColor: COLORS.border, borderRadius: 12, borderWidth: 1, color: COLORS.text, fontSize: 22, fontWeight: "800", letterSpacing: 5, minHeight: 52, paddingHorizontal: 12, textAlign: "center" }}
        testID="pairing-code-input"
        value={code}
      />
      <ActionButton
        busy={busy === "pair"}
        disabled={code.length !== 6 || Boolean(busy)}
        label="Jumeler cet iPhone"
        onPress={onPair}
        testID="pairing-confirm-button"
        variant="accent"
      />
    </>
  );
}


function AuthenticatedState({ counts }: { counts: Bootstrap["counts"] | undefined }) {
  if (!counts) return null;
  const entries = [
    ["Tâches", counts.tasks],
    ["Agents", counts.agents],
    ["Mémoires", counts.memory_items],
    ["Autorisations", counts.approvals_pending],
    ["Audit", counts.audit_events],
  ];
  return (
    <>
      <SectionTitle title="Données du serveur" />
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
        {entries.map(([label, value]) => (
          <View key={String(label)} style={{ gap: 3, minWidth: "28%", paddingVertical: 8 }}>
            <Text style={{ color: COLORS.accent, fontSize: 20, fontWeight: "800" }}>{value}</Text>
            <Text style={{ color: COLORS.muted, fontSize: 11 }}>{label}</Text>
          </View>
        ))}
      </View>
    </>
  );
}

function AuditJournal({
  audit,
  auditLoaded,
  error,
}: {
  audit: AuditEvent[];
  auditLoaded: boolean;
  error: string | null;
}) {
  return (
    <>
      <SectionTitle title="Journal d’audit" />
      <View>
        {audit.length ? audit.map((event) => (
          <View key={event.id} style={{ borderBottomColor: COLORS.border, borderBottomWidth: 1, gap: 3, paddingVertical: 8 }}>
            <Text selectable style={{ color: COLORS.text, fontSize: 12, fontWeight: "700" }}>{event.event_type}</Text>
            <Text selectable style={{ color: COLORS.subtle, fontFamily: "Courier", fontSize: 10 }} numberOfLines={1}>
              {event.hash?.slice(0, 16) ?? "sans hash"} · {timeAgo(event.created_at)}
            </Text>
          </View>
        )) : auditLoaded ? (
          <Text style={{ color: COLORS.subtle }}>Aucun événement authentifié à afficher.</Text>
        ) : (
          <Text style={{ color: COLORS.subtle }}>
            {error
              ? "Journal indisponible tant que l’accès authentifié n’est pas rétabli."
              : "Actualise pour charger le journal authentifié de cette connexion."}
          </Text>
        )}
      </View>
    </>
  );
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

function useAuthenticatedDashboard(setUrl: Dispatch<SetStateAction<string>>) {
  const requestEpoch = useRef(0);
  const [activeUrl, setActiveUrl] = useState<string | null>(null);
  const [paired, setPaired] = useState(false);
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [auditLoaded, setAuditLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshAuthenticatedData = useCallback(async (epoch: number) => {
    setPaired(false);
    setActiveUrl(null);
    setBootstrap(null);
    setAudit([]);
    setAuditLoaded(false);
    const isPaired = await hasDeviceToken();
    if (epoch !== requestEpoch.current) {
      return;
    }
    if (!isPaired) {
      return;
    }
    const [summary, events] = await Promise.all([
      bootstrapSync(() => epoch === requestEpoch.current),
      listAudit(15),
    ]);
    if (epoch !== requestEpoch.current) {
      return;
    }
    const currentUrl = await getServerUrl();
    if (epoch !== requestEpoch.current) {
      return;
    }
    setBootstrap(summary);
    setAudit(events);
    setAuditLoaded(true);
    setActiveUrl(currentUrl);
    setPaired(true);
  }, []);

  const refreshDashboard = useCallback(async () => {
    const epoch = ++requestEpoch.current;
    setRefreshing(true);
    setError(null);
    try {
      await refreshAuthenticatedData(epoch);
    } catch (cause) {
      if (epoch === requestEpoch.current) {
        setError(errorMessage(cause));
      }
    } finally {
      if (epoch === requestEpoch.current) {
        setRefreshing(false);
      }
    }
  }, [refreshAuthenticatedData]);

  const adoptVerifiedConnection = useCallback((summary: Bootstrap, currentUrl: string) => {
    requestEpoch.current += 1;
    setRefreshing(false);
    setError(null);
    setBootstrap(summary);
    setAudit([]);
    setAuditLoaded(false);
    setActiveUrl(currentUrl);
    setPaired(true);
  }, []);

  useEffect(() => {
    const epoch = ++requestEpoch.current;
    void (async () => {
      try {
        const currentUrl = await getServerUrl();
        if (epoch !== requestEpoch.current) {
          return;
        }
        setUrl(currentUrl);
        setRefreshing(true);
        setError(null);
        await refreshAuthenticatedData(epoch);
      } catch (cause) {
        if (epoch === requestEpoch.current) {
          setError(errorMessage(cause));
        }
      } finally {
        if (epoch === requestEpoch.current) {
          setRefreshing(false);
        }
      }
    })();
    return () => {
      requestEpoch.current += 1;
    };
  }, [refreshAuthenticatedData, setUrl]);

  return {
    activeUrl,
    audit,
    auditLoaded,
    adoptVerifiedConnection,
    bootstrap,
    error,
    paired,
    refreshDashboard,
    refreshing,
    setError,
  };
}

function usePairing({
  adoptVerifiedConnection,
  setError,
  setUrl,
  url,
}: {
  adoptVerifiedConnection: (summary: Bootstrap, currentUrl: string) => void;
  setError: Dispatch<SetStateAction<string | null>>;
  setUrl: Dispatch<SetStateAction<string>>;
  url: string;
}) {
  const [deviceName, setDeviceName] = useState("Mon iPhone");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState<"pair" | null>(null);
  const [notice, setNotice] = useState("Configure l’adresse, puis saisis un code généré localement sur le serveur.");

  async function pair() {
    if (code.length !== 6 || busy) return;
    setBusy("pair");
    setError(null);
    setNotice("Validation du jumelage et de l’accès authentifié…");
    try {
      const verifiedPairing = await pairConnection({
        code,
        deviceName: deviceName.trim() || "Mon iPhone",
        serverUrl: url,
      });
      setUrl(verifiedPairing.serverUrl);
      adoptVerifiedConnection(verifiedPairing.bootstrap, verifiedPairing.serverUrl);
      setCode("");
      setNotice("Jumelage réussi. Le jeton reste dans le trousseau sécurisé et n’est jamais affiché.");
    } catch (cause) {
      setNotice("Le jumelage n’a pas été confirmé par un accès authentifié complet.");
      setError(errorMessage(cause));
    } finally {
      setBusy(null);
    }
  }

  return {
    busy,
    code,
    deviceName,
    notice,
    pair,
    setCode,
    setDeviceName,
  };
}

type SettingsCategory = "connection" | "memory" | "team" | "journal";

export default function SettingsScreen() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [expandedCategory, setExpandedCategory] = useState<SettingsCategory | null>(null);
  const dashboard = useAuthenticatedDashboard(setUrl);
  const pairing = usePairing({
    adoptVerifiedConnection: dashboard.adoptVerifiedConnection,
    setError: dashboard.setError,
    setUrl,
    url,
  });
  useAccessibilityAnnouncement(pairing.notice);
  const toggleCategory = (category: SettingsCategory) => {
    setExpandedCategory((current) => current === category ? null : category);
  };
  const connectionSummary = dashboard.paired && dashboard.activeUrl
    ? `Connexion authentifiée : ${dashboard.activeUrl}`
    : dashboard.refreshing ? "Vérification de la connexion…" : "Connexion non authentifiée ou non vérifiée";

  return (
    <ScreenShell
      title="Réglages"
      showTitle={false}
      subtitle="Personnalise monGARS selon tes besoins."
      onRefresh={() => void dashboard.refreshDashboard()}
      refreshing={dashboard.refreshing}
      testID="settings-screen"
    >
      <View style={{ gap: 10 }}>
        <SectionTitle title="Connexion" />
        <SettingsGroup>
          <SettingsDisclosure
            title="Serveur et jumelage"
            description={connectionSummary}
            icon="server"
            grouped
            expanded={expandedCategory === "connection"}
            onToggle={() => toggleCategory("connection")}
            testID="settings-connection-toggle"
          >
            <ErrorBanner message={dashboard.error} />
            {pairing.busy || pairing.notice !== "Configure l’adresse, puis saisis un code généré localement sur le serveur." ? (
              <Text accessibilityLiveRegion="polite" selectable style={{ color: COLORS.muted, fontSize: 13, lineHeight: 19 }}>{pairing.notice}</Text>
            ) : null}
            <ControlPlaneSection activeUrl={dashboard.activeUrl} onUrlChange={setUrl} paired={dashboard.paired} url={url} />
            <PairingSection
              busy={pairing.busy}
              code={pairing.code}
              deviceName={pairing.deviceName}
              onCodeChange={(value) => pairing.setCode(value.replace(/\D/g, ""))}
              onDeviceNameChange={pairing.setDeviceName}
              onPair={() => void pairing.pair()}
            />
          </SettingsDisclosure>
        </SettingsGroup>
        {dashboard.error ? <Text accessibilityRole="alert" style={{ color: COLORS.warning, fontSize: 13, lineHeight: 19 }}>La connexion n’a pas pu être vérifiée. Ouvre « Serveur et jumelage » pour rétablir l’accès.</Text> : null}
      </View>

      <View style={{ gap: 10 }}>
        <SectionTitle title="Intelligence" />
        <SettingsGroup>
          <SettingsNavigationRow
            title="Modèles locaux"
            icon="model"
            grouped
            onPress={() => router.push("/local-model")}
            testID="open-local-model-button"
          />
          <SettingsDisclosure title="Mémoire" icon="memory" grouped expanded={expandedCategory === "memory"} onToggle={() => toggleCategory("memory")} testID="settings-memory-toggle">
            <ActionButton label="Consulter la mémoire" onPress={() => router.push("/memory")} testID="settings-open-memory" />
            <SemanticMemoryPanel />
          </SettingsDisclosure>
          <SettingsDisclosure title="Équipe et compétences" icon="team" grouped expanded={expandedCategory === "team"} onToggle={() => toggleCategory("team")} testID="settings-server-models-toggle">
            <Text style={{ color: COLORS.muted, lineHeight: 20 }}>Consulte les agents du serveur, leurs modèles et les outils disponibles. Les modèles de cet iPhone se règlent séparément dans « Modèles locaux ».</Text>
            <ActionButton label="Voir les agents" onPress={() => router.push("/agents")} testID="settings-open-agents" />
            <ActionButton label="Explorer les compétences" onPress={() => router.push("/catalog")} testID="settings-open-catalog" />
          </SettingsDisclosure>
        </SettingsGroup>
      </View>

      <View style={{ gap: 10 }}>
        <SectionTitle title="Confidentialité et suivi" />
        <SettingsGroup>
          <SettingsNavigationRow title="Autorisations" icon="authorization" grouped onPress={() => router.push("/approvals")} testID="settings-open-approvals" />
          <SettingsDisclosure title="Journal" icon="journal" grouped expanded={expandedCategory === "journal"} onToggle={() => toggleCategory("journal")} testID="settings-diagnostics-toggle">
            <ActionButton label="Actualiser le journal" busy={dashboard.refreshing} onPress={() => void dashboard.refreshDashboard()} />
            <AuthenticatedState counts={dashboard.bootstrap?.counts} />
            <AuditJournal audit={dashboard.audit} auditLoaded={dashboard.auditLoaded} error={dashboard.error} />
          </SettingsDisclosure>
        </SettingsGroup>
      </View>
    </ScreenShell>
  );
}
