import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { Text, TextInput, View } from "react-native";
import * as SecureStore from "expo-secure-store";
import { useRouter } from "expo-router";

import { ScreenShell } from "@/components/screen-shell";
import { ActionButton, Card, COLORS, ErrorBanner, SectionTitle, timeAgo, useAccessibilityAnnouncement } from "@/components/swarm-ui";
import {
  bootstrapSync,
  getServerUrl,
  hasDeviceToken,
  listAudit,
  pairDevice,
  type AuditEvent,
  type Bootstrap,
} from "@/lib/api/client";

const DEVICE_ID_KEY = "mongars.device_id";

async function getDeviceId(): Promise<string> {
  let id = await SecureStore.getItemAsync(DEVICE_ID_KEY);
  if (!id) {
    id = `iphone_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    await SecureStore.setItemAsync(DEVICE_ID_KEY, id);
  }
  return id;
}

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
      <SectionTitle title="Control plane" />
      <Card>
        <Text style={{ color: COLORS.muted, fontSize: 13 }}>
          HTTPS est obligatoire hors de la boucle locale. L’adresse n’est enregistrée qu’après un jumelage réussi.
        </Text>
        <Text style={{ color: COLORS.muted, fontSize: 12, fontWeight: "700" }}>
          Adresse du control plane
        </Text>
        <TextInput
          accessibilityLabel="Adresse du control plane"
          autoCapitalize="none"
          autoCorrect={false}
          onChangeText={onUrlChange}
          placeholder="https://control-plane.example"
          placeholderTextColor={COLORS.subtle}
          style={{ backgroundColor: COLORS.background, borderColor: COLORS.border, borderRadius: 12, borderWidth: 1, color: COLORS.text, minHeight: 46, paddingHorizontal: 12 }}
          testID="server-url-input"
          value={url}
        />
        <Text style={{ color: paired ? COLORS.accent : COLORS.warning, fontWeight: "700" }}>
          {paired && activeUrl
            ? `Connexion authentifiée : ${activeUrl}`
            : "Connexion non authentifiée ou non vérifiée"}
        </Text>
        {paired && activeUrl && candidateUrl !== activeUrl ? (
          <Text accessibilityRole="alert" style={{ color: COLORS.warning, lineHeight: 19 }}>
            Cette adresse est une candidate non vérifiée. La connexion active reste {activeUrl} jusqu’à un nouveau jumelage réussi.
          </Text>
        ) : null}
      </Card>
    </>
  );
}

function PairingSection({
  busy,
  code,
  deviceName,
  notice,
  onCodeChange,
  onDeviceNameChange,
  onPair,
}: {
  busy: "pair" | null;
  code: string;
  deviceName: string;
  notice: string;
  onCodeChange: (value: string) => void;
  onDeviceNameChange: (value: string) => void;
  onPair: () => void;
}) {
  return (
    <>
      <SectionTitle title="Jumelage" />
      <Card>
        <Text style={{ color: COLORS.muted, lineHeight: 20 }}>
          1. Sur le serveur, ouvre la procédure de jumelage de l’installation locale.
        </Text>
        <Text style={{ color: COLORS.muted, lineHeight: 20 }}>
          2. Depuis la boucle locale du serveur, l’opérateur génère un code temporaire à six chiffres.
        </Text>
        <Text style={{ color: COLORS.muted, lineHeight: 20 }}>
          3. Sur l’iPhone, saisis uniquement l’adresse du control plane et ce code temporaire.
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
        <Text accessibilityLiveRegion="polite" selectable style={{ color: COLORS.muted, lineHeight: 19 }}>
          {notice}
        </Text>
      </Card>
    </>
  );
}

function AuthenticatedState({ counts }: { counts: Bootstrap["counts"] | undefined }) {
  if (!counts) return null;
  const entries = [
    ["Tâches", counts.tasks],
    ["Agents", counts.agents],
    ["Mémoires", counts.memory_items],
    ["Accords", counts.approvals_pending],
    ["Audit", counts.audit_events],
  ];
  return (
    <>
      <SectionTitle title="État authentifié" />
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
        {entries.map(([label, value]) => (
          <Card key={String(label)} style={{ alignItems: "center", minWidth: "30%" }}>
            <Text style={{ color: COLORS.accent, fontSize: 20, fontWeight: "800" }}>{value}</Text>
            <Text style={{ color: COLORS.muted, fontSize: 11 }}>{label}</Text>
          </Card>
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
      <Card>
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
      </Card>
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
      const verifiedPairing = await pairDevice(
        code,
        await getDeviceId(),
        deviceName.trim() || "Mon iPhone",
        url,
      );
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

export default function SettingsScreen() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const dashboard = useAuthenticatedDashboard(setUrl);
  const pairing = usePairing({
    adoptVerifiedConnection: dashboard.adoptVerifiedConnection,
    setError: dashboard.setError,
    setUrl,
    url,
  });
  useAccessibilityAnnouncement(pairing.notice);

  return (
    <ScreenShell
      title="Réglages"
      subtitle="Connexion locale, jumelage externe et preuves d’audit."
      onRefresh={() => void dashboard.refreshDashboard()}
      refreshing={dashboard.refreshing}
      testID="settings-screen"
    >
      <ErrorBanner message={dashboard.error} />
      <SectionTitle title="Inférence sur l’iPhone" />
      <Card>
        <Text selectable style={{ color: COLORS.muted, lineHeight: 20 }}>
          Importe ou charge un modèle Core ML, MLX ou GGUF. L’intention et la sortie restent sur l’iPhone pendant la génération; une proposition n’est envoyée au control plane qu’après une action explicite.
        </Text>
        <ActionButton
          label="Ouvrir les modèles locaux"
          onPress={() => router.push("/local-model")}
          testID="open-local-model-button"
        />
      </Card>
      <ControlPlaneSection
        activeUrl={dashboard.activeUrl}
        onUrlChange={setUrl}
        paired={dashboard.paired}
        url={url}
      />
      <PairingSection
        busy={pairing.busy}
        code={pairing.code}
        deviceName={pairing.deviceName}
        notice={pairing.notice}
        onCodeChange={(value) => pairing.setCode(value.replace(/\D/g, ""))}
        onDeviceNameChange={pairing.setDeviceName}
        onPair={() => void pairing.pair()}
      />
      <AuthenticatedState counts={dashboard.bootstrap?.counts} />
      <AuditJournal
        audit={dashboard.audit}
        auditLoaded={dashboard.auditLoaded}
        error={dashboard.error}
      />
    </ScreenShell>
  );
}
