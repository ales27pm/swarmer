import { useCallback, useEffect, useRef, useState } from "react";
import { AppState, Image, Linking, Pressable, Text, TextInput, View } from "react-native";
import { ScreenShell } from "@/components/screen-shell";
import { ActionButton, Card, COLORS, ErrorBanner, SectionTitle } from "@/components/swarm-ui";
import { commandWebsiteProject, createWebsiteProject, getWebsiteCapabilities, listWebsiteProjects, prepareWebsitePublication, previewWebsiteProject, previewWebsiteScreenshot, publishWebsiteProject, type WebsiteApproval, type WebsiteCapabilities, type WebsiteProject } from "@/lib/application-api/server";
import { subscribeConnectionChanges } from "@/lib/connection-events";

const LABELS: Record<WebsiteProject["status"], string> = { draft: "Prêt à explorer", capturing: "Exploration en cours", captured: "Source capturée", branding: "Recherche visuelle en cours", awaiting_direction: "Direction à choisir", building: "Reconstruction en cours", preview_ready: "Aperçu prêt", publishing: "Publication en cours", published: "Publication enregistrée", failed: "Étape à reprendre", interrupted: "Étape interrompue" };
const BLOCKERS: Record<string, string> = { source_capture_partial: "Certaines pages restent à explorer.", source_extraction_truncated: "Du contenu dépasse les limites de capture.", source_forms_require_implementation: "Les formulaires nécessitent un raccordement avant utilisation.", migration_items_need_confirmation: "Des contenus ou médias restent à confirmer.", palette_contrast_below_4_5: "Certains contrastes doivent être ajustés." };
const working = (p: WebsiteProject | null) => p && ["capturing", "branding", "building", "publishing"].includes(p.status);
const screenshotIdentity = (p: WebsiteProject | null) => JSON.stringify(p?.capture?.screenshots.map((shot) => JSON.stringify([shot.url, shot.viewport, shot.sha256, shot.path])).sort() ?? []);
const requestId = () => `website-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
const fieldStyle = { borderWidth: 1, borderColor: COLORS.border, borderRadius: 12, color: COLORS.text, padding: 14, fontSize: 16 };

function StrategyDetails({ analysis }: { analysis: Record<string, unknown> }) {
  const proposals = Array.isArray(analysis.proposals) ? analysis.proposals.filter((value): value is string => typeof value === "string") : [];
  const pages = Array.isArray(analysis.page_proposals) ? analysis.page_proposals : [];
  return <View style={{ gap: 12 }}>
    <Text style={{ color: COLORS.text, fontWeight: "600" }}>Propositions à valider avec l’entreprise</Text>
    {proposals.map((text, index) => <Text key={index} style={{ color: COLORS.muted }}>• {text}</Text>)}
    {pages.slice(0, 30).map((raw, index) => {
      if (!raw || typeof raw !== "object") return null;
      const page = raw as Record<string, unknown>;
      const action = page.primary_action && typeof page.primary_action === "object" ? page.primary_action as Record<string, unknown> : null;
      return <View key={index} style={{ gap: 4 }}>
        <Text selectable style={{ color: COLORS.text }}>{typeof page.source_url === "string" ? page.source_url : `Page ${index + 1}`}</Text>
        <Text style={{ color: COLORS.muted }}>Action proposée : {typeof action?.label === "string" ? action.label : "à définir avec l’entreprise"}</Text>
      </View>;
    })}
    <Text style={{ color: COLORS.muted }}>Le public cible, les avantages concurrentiels et les objectifs commerciaux restent à confirmer.</Text>
  </View>;
}

export default function WebsiteScreen() {
  const [projects, setProjects] = useState<WebsiteProject[]>([]);
  const [capabilities, setCapabilities] = useState<WebsiteCapabilities | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [source, setSource] = useState("");
  const [objective, setObjective] = useState("");
  const [palette, setPalette] = useState<string | null>(null);
  const [direction, setDirection] = useState<"editorial" | "studio" | "catalog">("editorial");
  const [brandingDetails, setBrandingDetails] = useState(false);
  const [strategyDetails, setStrategyDetails] = useState(false);
  const [showCaptures, setShowCaptures] = useState(false);
  const [screenshot, setScreenshot] = useState<string | null>(null);
  const [approval, setApproval] = useState<WebsiteApproval | null>(null);
  const [reviewed, setReviewed] = useState<string | null>(null);
  const [previewOpened, setPreviewOpened] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scope = useRef(0), refreshSerial = useRef(0), active = useRef(AppState.currentState === "active");
  const operationSerial = useRef(0), operationBusy = useRef(false);
  const previewAttempt = useRef(0);
  const creationId = useRef(requestId());
  const project = projects.find((p) => p.id === selected) ?? null;
  const captureIdentity = screenshotIdentity(project);
  const latestProject = useRef(project);
  latestProject.current = project;
  const current = useCallback(() => { const epoch = scope.current; return () => epoch === scope.current && active.current; }, []);
  const update = useCallback((data: WebsiteProject) => setProjects((items) => {
    if (items.some((item) => item.id === data.id && item.version > data.version)) return items;
    return [data, ...items.filter((item) => item.id !== data.id)];
  }), []);
  const refresh = useCallback(async (showProgress = false) => {
    const accept = current(), serial = ++refreshSerial.current;
    if (showProgress) setRefreshing(true);
    try {
      const [next, caps] = await Promise.all([listWebsiteProjects(accept), getWebsiteCapabilities(accept)]);
      if (!accept() || serial !== refreshSerial.current) return;
      setProjects((previous) => next.map((item) => {
        const currentItem = previous.find((candidate) => candidate.id === item.id);
        return currentItem && currentItem.version > item.version ? currentItem : item;
      })); setCapabilities(caps); setError(null);
    } catch (cause) { if (accept() && serial === refreshSerial.current) setError(cause instanceof Error ? cause.message : "Impossible de lire les projets web."); }
    finally { if (accept() && serial === refreshSerial.current) setRefreshing(false); }
  }, [current]);
  useEffect(() => {
    const unsubscribe = subscribeConnectionChanges(() => {
      scope.current += 1; refreshSerial.current += 1;
      previewAttempt.current += 1;
      operationSerial.current += 1; operationBusy.current = false;
      setProjects([]); setCapabilities(null); setSelected(null); setApproval(null); setReviewed(null); setBusy(false); setRefreshing(false);
      setPreviewOpened(null);
      setScreenshot(null);
      setError("La connexion a changé. Ton brouillon est conservé ; actualise les projets de ce serveur.");
    });
    const listener = AppState.addEventListener("change", (state) => {
      active.current = state === "active";
      if (!active.current) { scope.current += 1; operationSerial.current += 1; operationBusy.current = false; setApproval(null); setBusy(false); setRefreshing(false); }
      else void refresh();
    });
    void refresh();
    return () => { unsubscribe(); listener.remove(); scope.current += 1; previewAttempt.current += 1; };
  }, [refresh]);
  useEffect(() => {
    setApproval((ticket) => ticket && project?.build?.digest === ticket.build_digest && project.version === ticket.expected_version ? ticket : null);
    setReviewed((value) => value === project?.build?.digest ? value : null);
    setPreviewOpened((value) => value === project?.build?.digest ? value : null);
  }, [project?.id, project?.version, project?.build?.digest, approval?.expected_version, approval?.build_digest]);
  useEffect(() => { setScreenshot(null); setShowCaptures(false); }, [project?.id, captureIdentity]);
  useEffect(() => { setBrandingDetails(false); setStrategyDetails(false); }, [project?.id]);
  useEffect(() => {
    if (!working(project) || busy) return;
    const timer = setInterval(() => { if (active.current) void refresh(); }, 3000);
    return () => clearInterval(timer);
  }, [project, busy, refresh]);

  async function perform(operation: (accept: () => boolean) => Promise<void>) {
    if (busy || operationBusy.current || !active.current) return;
    const operationId = ++operationSerial.current;
    operationBusy.current = true;
    const accept = current(); refreshSerial.current += 1; setBusy(true); setRefreshing(false); setError(null);
    try { await operation(accept); }
    catch (cause) { if (accept()) setError(cause instanceof Error ? cause.message : "Cette étape n’a pas pu être confirmée."); }
    finally {
      if (operationId === operationSerial.current) {
        operationBusy.current = false;
        refreshSerial.current += 1;
        if (accept()) { setBusy(false); setRefreshing(false); }
      }
    }
  }
  const create = () => perform(async (accept) => {
    const data = await createWebsiteProject({ request_id: creationId.current, source_url: source.trim(), objective: objective.trim() }, accept);
    if (accept()) { update(data); setSelected(data.id); setReviewed(null); setApproval(null); setPalette(null); setDirection("editorial"); creationId.current = requestId(); }
  });
  const command = (action: "capture" | "branding" | "build") => perform(async (accept) => {
    if (!project) return;
    const data = await commandWebsiteProject(project.id, { request_id: requestId(), expected_version: project.version, action, ...(action === "build" ? { palette_id: palette ?? undefined, direction_id: direction } : {}) }, accept);
    if (accept()) { update(data); setApproval(null); setReviewed(null); }
  });
  const openPreview = () => perform(async (accept) => {
    if (!project?.build) return;
    const attempt = ++previewAttempt.current;
    const preview = await previewWebsiteProject(project.id, { expected_version: project.version, build_digest: project.build.digest }, accept);
    if (accept()) {
      setPreviewOpened(null); setReviewed(null); setApproval(null);
      await Linking.openURL(preview.url);
      // A successful browser handoff can background the app before it resolves.
      if (attempt === previewAttempt.current && latestProject.current?.id === project.id && latestProject.current?.build?.digest === preview.build_digest) setPreviewOpened(preview.build_digest);
    }
  });
  const prepare = () => perform(async (accept) => {
    if (!project?.build) return;
    const next = await prepareWebsitePublication(project.id, { expected_version: project.version, build_digest: project.build.digest }, accept);
    if (accept()) { setApproval(next); update({ ...project, version: next.expected_version }); }
  });
  const publish = () => perform(async (accept) => {
    if (!project || !approval) return;
    const result = await publishWebsiteProject(project.id, { expected_version: approval.expected_version, build_digest: approval.build_digest, approval_token: approval.approval_token, confirm_publication: true }, accept);
    if (accept()) { update(result); setApproval(null); }
  });

  return <ScreenShell title="Sites et identité" subtitle="Du site existant à une refonte que tu peux vérifier." onRefresh={() => void refresh(true)} refreshing={refreshing} testID="website-screen">
    <ErrorBanner message={error} />
    {project ? <>
      <ActionButton label="Tous les projets web" disabled={busy} onPress={() => { setSelected(null); setApproval(null); setReviewed(null); }} />
      <View style={{ gap: 8 }}>
        <SectionTitle title={project.objective} />
        <Text selectable style={{ color: COLORS.muted }}>{project.source_url}</Text>
        <Text accessibilityLiveRegion="polite" style={{ color: COLORS.accent, fontWeight: "700" }}>{LABELS[project.status]}</Text>
        <ErrorBanner message={project.error} />
      </View>
      <Card>
        <SectionTitle title="1. Explorer le site d’origine" />
        <Text style={{ color: COLORS.muted, lineHeight: 21 }}>Les pages accessibles, leurs textes et leurs médias deviennent la matière première de la refonte.</Text>
        {project.capture ? <>
          <Text style={{ color: COLORS.text }}>{project.capture.pages} pages · {project.capture.inventory_items} contenus · {project.capture.assets} médias</Text>
          <Text style={{ color: COLORS.muted }}>{project.capture.coverage.status === "partial" ? "Capture partielle — consulte les détails avant publication." : "Parcours terminé dans les limites de cette capture."}</Text>
          <Text style={{ color: COLORS.muted }}>{project.capture.rendered_pages} pages observées dans le navigateur · {project.capture.screenshots.length} captures d’écran</Text>
          {project.capture.screenshots.length ? <ActionButton label={showCaptures ? "Masquer les captures" : "Voir les captures d’écran"} onPress={() => setShowCaptures(!showCaptures)} /> : null}
          {showCaptures ? project.capture.screenshots.map((shot) => <ActionButton key={shot.sha256 + shot.viewport} label={`${shot.viewport === "mobile" ? "Téléphone" : "Ordinateur"} · ${shot.url}`} disabled={busy} onPress={() => void perform(async (accept) => {
            const acceptsShot = () => accept() && latestProject.current?.id === project.id && screenshotIdentity(latestProject.current) === captureIdentity;
            const result = await previewWebsiteScreenshot(project.id, shot.sha256, acceptsShot);
            if (acceptsShot()) setScreenshot(result.url);
          })} />) : null}
          {showCaptures && screenshot ? <Image accessibilityLabel="Capture du site source" source={{ uri: screenshot }} resizeMode="contain" style={{ width: "100%", height: 480 }} onError={() => { setScreenshot(null); setError("Cette capture a expiré. Ouvre-la à nouveau."); }} /> : null}
        </> : null}
        <ActionButton label={project.capture ? "Actualiser la source" : "Explorer le site"} disabled={Boolean(working(project)) || busy} onPress={() => void command("capture")} />
      </Card>
      {project.capture ? <Card>
        <SectionTitle title="2. Choisir une direction" />
        <Text style={{ color: COLORS.muted, lineHeight: 21 }}>Infographic Artist recherche des références et propose une direction à partir du contenu source. Tu choisis ensuite la composition et la palette.</Text>
        <ActionButton label="Consulter Infographic Artist" disabled={!capabilities?.branding_configured || Boolean(working(project)) || busy} onPress={() => void command("branding")} />
        {!capabilities?.branding_configured ? <Text style={{ color: COLORS.warning }}>Le service Infographic Artist doit être connecté au serveur pour cette consultation.</Text> : null}
        {project.branding ? <><Text style={{ color: COLORS.text }}>Retour d’Infographic Artist disponible</Text><Pressable accessibilityRole="button" onPress={() => setBrandingDetails(!brandingDetails)}><Text style={{ color: COLORS.accent }}>{brandingDetails ? "Masquer les recommandations" : "Lire les recommandations"}</Text></Pressable>{brandingDetails ? <Text selectable style={{ color: COLORS.muted }}>{project.branding.summary}</Text> : null}</> : null}
        <Text style={{ color: COLORS.text, fontWeight: "600" }}>Composition</Text>
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
          {([ ["editorial", "Éditoriale"], ["studio", "Studio"], ["catalog", "Catalogue"] ] as const).map(([id, label]) => <Pressable key={id} accessibilityRole="radio" accessibilityState={{ checked: direction === id }} onPress={() => setDirection(id)} style={{ padding: 12, borderRadius: 12, borderWidth: 1, borderColor: direction === id ? COLORS.accent : COLORS.border }}><Text style={{ color: COLORS.text }}>{label}</Text></Pressable>)}
        </View>
        <Text style={{ color: COLORS.text, fontWeight: "600" }}>Palette du site</Text>
        {capabilities?.palettes.map((p) => <Pressable key={p.id} accessibilityRole="radio" accessibilityLabel={p.name} accessibilityState={{ checked: palette === p.id }} onPress={() => setPalette(p.id)} style={{ padding: 14, borderWidth: 2, borderRadius: 12, backgroundColor: p.background, borderColor: palette === p.id ? COLORS.accent : COLORS.border, flexDirection: "row", alignItems: "center", gap: 12 }}><View style={{ width: 24, height: 24, borderRadius: 12, backgroundColor: p.accent }} /><Text style={{ color: p.text, fontWeight: "600", flex: 1 }}>{p.name}{palette === p.id ? " · choisie" : ""}</Text></Pressable>)}
        <ActionButton label="Construire l’aperçu" disabled={!palette || Boolean(working(project)) || busy} onPress={() => void command("build")} />
      </Card> : null}
      {project.build ? <Card>
        <SectionTitle title="3. Vérifier la refonte" />
        <Text style={{ color: COLORS.text }}>{project.build.migration.accounted_count}/{project.build.migration.inventory_count} contenus répertoriés dans le rapport de migration.</Text>
        <Text style={{ color: COLORS.muted }}>Chaque contenu indique sa destination ou la raison pour laquelle il reste à confirmer.</Text>
        {project.build.readiness.blockers.map((blocker) => <Text key={blocker} style={{ color: COLORS.warning }}>• {BLOCKERS[blocker] ?? blocker}</Text>)}
        <ActionButton label="Ouvrir l’aperçu du site" disabled={Boolean(working(project)) || busy} onPress={() => void openPreview()} />
        <Text style={{ color: COLORS.muted }}>Le navigateur ouvre un lien privé de cinq minutes, sans accès à ton compte Swarmer.</Text>
        <Pressable testID="website-preview-reviewed" accessibilityRole="checkbox" accessibilityLabel="J’ai vérifié cet aperçu et les points à confirmer." accessibilityState={{ checked: reviewed === project.build.digest, disabled: previewOpened !== project.build.digest || busy }} disabled={previewOpened !== project.build.digest || busy} onPress={() => setReviewed(reviewed === project.build?.digest ? null : project.build?.digest ?? null)} style={{ paddingVertical: 14 }}>
          <Text style={{ color: previewOpened === project.build.digest ? COLORS.text : COLORS.subtle }}>{reviewed === project.build.digest ? "☑" : "☐"} J’ai vérifié cet aperçu et les points à confirmer.</Text>
        </Pressable>
        <Pressable accessibilityRole="button" onPress={() => setStrategyDetails(!strategyDetails)}><Text style={{ color: COLORS.accent }}>{strategyDetails ? "Masquer les détails" : "Stratégie et traçabilité"}</Text></Pressable>
        {strategyDetails ? <StrategyDetails analysis={project.build.strategy.marketing_analysis} /> : null}
        <SectionTitle title="4. Publier la version vérifiée" />
        {!capabilities?.publication_configured ? <Text style={{ color: COLORS.warning }}>Une destination de publication doit être configurée sur le serveur.</Text> : <Text style={{ color: COLORS.muted }}>Destination : {capabilities.publication_target}</Text>}
        {!approval ? <ActionButton label="Préparer la publication" disabled={!capabilities?.publication_configured || reviewed !== project.build.digest || busy || Boolean(working(project))} onPress={() => void prepare()} /> : <>
          <Text style={{ color: COLORS.text }}>Confirme la publication de cette version sur {approval.target}.</Text>
          <Text selectable style={{ color: COLORS.muted }}>Version : {approval.build_digest.slice(0, 16)}</Text>
          <ActionButton label="Confirmer la publication" disabled={busy} onPress={() => void publish()} />
          <ActionButton label="Annuler" disabled={busy} onPress={() => setApproval(null)} />
        </>}
        {project.publication ? <Text selectable style={{ color: COLORS.accent }}>Dernière version publiée : {project.publication.url}</Text> : null}
      </Card> : null}
    </> : <>
      <Card>
        <SectionTitle title="Nouveau projet web" />
        <Text style={{ color: COLORS.muted }}>Commence par l’adresse du site de l’entreprise et le résultat souhaité.</Text>
        <TextInput accessibilityLabel="Adresse du site d’origine" testID="website-source" placeholder="https://entreprise.ca" placeholderTextColor={COLORS.subtle} value={source} onChangeText={(v) => { setSource(v); creationId.current = requestId(); }} autoCapitalize="none" keyboardType="url" autoCorrect={false} style={fieldStyle} />
        <TextInput accessibilityLabel="Objectif de la refonte" testID="website-objective" placeholder="Clarifier nos services et moderniser notre image" placeholderTextColor={COLORS.subtle} value={objective} onChangeText={(v) => { setObjective(v); creationId.current = requestId(); }} multiline style={[fieldStyle, { minHeight: 90 }]} />
        <ActionButton label="Créer le projet web" disabled={!capabilities || !source.trim() || !objective.trim() || busy} busy={busy} onPress={() => void create()} />
      </Card>
      <SectionTitle title="Reprendre un projet web" />
      {!projects.length ? <Text style={{ color: COLORS.muted }}>Tes projets web apparaîtront ici.</Text> : projects.map((p) => <Pressable key={p.id} accessibilityRole="button" accessibilityState={{ disabled: busy }} disabled={busy} onPress={() => { setSelected(p.id); setPalette(p.build?.palette.id ?? null); setDirection(p.build?.strategy.selected_direction?.layout ?? "editorial"); setApproval(null); setReviewed(null); }}><Card><Text style={{ color: COLORS.text, fontWeight: "700", fontSize: 17 }}>{p.objective}</Text><Text style={{ color: COLORS.muted }}>{p.source_url}</Text><Text style={{ color: COLORS.accent }}>{LABELS[p.status]}</Text></Card></Pressable>)}
    </>}
  </ScreenShell>;
}
