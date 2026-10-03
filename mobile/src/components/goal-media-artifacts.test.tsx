import { act, render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState, type AppStateStatus } from "react-native";
import { setAudioModeAsync } from "expo-audio";
import { GoalMediaArtifacts } from "./goal-media-artifacts";
import { getGoalMediaArtifacts, getGoalMediaBytes, type PlanNode } from "@/lib/api/client";
import { createGoalMediaCache } from "@/lib/goal-media-cache";
import { notifyConnectionChanged } from "@/lib/connection-events";
import { mediaFixture, mediaGoalId, mediaJobId } from "@/testing/goal-media-fixtures";

jest.mock("@/lib/api/client", () => ({ getGoalMediaArtifacts: jest.fn(), getGoalMediaBytes: jest.fn() }));
jest.mock("@/lib/goal-media-cache", () => ({ createGoalMediaCache: jest.fn() }));
const mockPlayer = { playing: false, currentTime: 0, duration: 1, pause: jest.fn(), play: jest.fn(), seekTo: jest.fn(async () => undefined) };
const mockStatus = { isLoaded: true, playing: false, currentTime: 0, duration: 1, didJustFinish: false, playbackState: "ready" };
let mockBlur: (() => void) | undefined;
jest.mock("expo-router", () => ({ useFocusEffect: (effect: () => (() => void)) => {
  const React = jest.requireActual<typeof import("react")>("react");
  React.useEffect(() => { const cleanup = effect(); mockBlur = cleanup; return cleanup; }, [effect]);
} }));
jest.mock("expo-audio", () => ({
  useAudioPlayer: jest.fn(() => mockPlayer), useAudioPlayerStatus: jest.fn(() => mockStatus),
  setAudioModeAsync: jest.fn(async () => undefined),
}));
const image = mediaFixture(); const audio = mediaFixture(true);
const node: PlanNode = { id: "node_media", goal_run_id: mediaGoalId, node_type: "worker", title: "Créer une image", objective: "Image",
  required_skill: "image.generate", status: "completed", priority: 80, depends_on: [], worker_job_id: mediaJobId,
  created_at: "2026-10-02T00:00:00Z", updated_at: "2026-10-02T00:00:00Z" };
const props = { goalId: mediaGoalId, nodes: [node], enabled: true, visible: true };
const mockPut = jest.fn(() => "file:///private/cache/preview.png");
const mockDispose = jest.fn();
let background: (state: AppStateStatus) => void;
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }
beforeEach(() => {
  jest.clearAllMocks();
  jest.mocked(getGoalMediaArtifacts).mockResolvedValue([image.artifact]);
  jest.mocked(getGoalMediaBytes).mockResolvedValue(image.bytes);
  jest.mocked(createGoalMediaCache).mockReturnValue({ put: mockPut, dispose: mockDispose });
  mockPlayer.playing = false; mockStatus.playing = false;
  mockPlayer.play.mockImplementation(() => { mockPlayer.playing = true; mockStatus.playing = true; });
  mockPlayer.pause.mockImplementation(() => { mockPlayer.playing = false; mockStatus.playing = false; });
  jest.spyOn(AppState, "addEventListener").mockImplementation((_event, listener) => { background = listener; return { remove: jest.fn() }; });
});
describe("project media results", () => {
  it("only fetches when the Results tab is visible and authoritative", async () => {
    await render(<GoalMediaArtifacts {...props} visible={false} />);
    expect(getGoalMediaArtifacts).not.toHaveBeenCalled();
    await screen.rerender(<GoalMediaArtifacts {...props} enabled={false} />);
    expect(getGoalMediaArtifacts).not.toHaveBeenCalled();
    await screen.rerender(<GoalMediaArtifacts {...props} />);
    const preview = await screen.findByRole("image", { name: "Image générée par ce projet" });
    expect(preview).toHaveProp("source", { uri: "file:///private/cache/preview.png" });
    expect(getGoalMediaBytes).toHaveBeenCalledWith(image.artifact, expect.any(Function), expect.anything());
  });
  it("shows generation and failure without fetching uncompleted jobs", async () => {
    await render(<GoalMediaArtifacts {...props} nodes={[{ ...node, status: "running" }]} />);
    expect(screen.getByText("Génération du média en cours…")).toBeOnTheScreen();
    expect(getGoalMediaArtifacts).not.toHaveBeenCalled();
    await screen.rerender(<GoalMediaArtifacts {...props} nodes={[{ ...node, status: "failed", error_summary: "Le moteur est indisponible." }]} />);
    expect(screen.getByText("La génération du média a échoué.")).toBeOnTheScreen();
    expect(screen.getByText("Le moteur est indisponible.")).toBeOnTheScreen();
  });
  it("uses local audio only, starts on explicit play and pauses on leaving Results", async () => {
    jest.mocked(getGoalMediaArtifacts).mockResolvedValue([audio.artifact]); mockPut.mockReturnValueOnce("file:///private/cache/audio.wav");
    await render(<GoalMediaArtifacts {...props} />);
    const play = await screen.findByRole("button", { name: "Lire l’audio" });
    expect(mockPlayer.play).not.toHaveBeenCalled();
    await userEvent.setup().press(play);
    expect(setAudioModeAsync).toHaveBeenCalledWith(expect.objectContaining({ allowsRecording: false, shouldPlayInBackground: false }));
    expect(mockPlayer.play).toHaveBeenCalledTimes(1);
    // The native player normally emits this status update through its hook.
    await screen.rerender(<GoalMediaArtifacts {...props} />);
    await userEvent.setup().press(await screen.findByRole("button", { name: "Pause" }));
    expect(mockPlayer.pause).toHaveBeenCalledTimes(1);
    await screen.rerender(<GoalMediaArtifacts {...props} visible={false} />);
    expect(mockPlayer.pause).toHaveBeenCalled(); expect(mockDispose).toHaveBeenCalledTimes(1);
  });
  it("does not start delayed playback after the preview has been closed", async () => {
    const waiting = deferred<void>(); jest.mocked(setAudioModeAsync).mockReturnValueOnce(waiting.promise);
    jest.mocked(getGoalMediaArtifacts).mockResolvedValue([audio.artifact]);
    await render(<GoalMediaArtifacts {...props} />);
    await userEvent.setup().press(await screen.findByRole("button", { name: "Lire l’audio" }));
    await screen.rerender(<GoalMediaArtifacts {...props} visible={false} />);
    await act(async () => waiting.resolve()); expect(mockPlayer.play).not.toHaveBeenCalled();
  });
  it("retains only one preview when switching between image and audio", async () => {
    jest.mocked(getGoalMediaArtifacts).mockResolvedValue([image.artifact, audio.artifact]);
    await render(<GoalMediaArtifacts {...props} />); await screen.findByRole("image");
    await userEvent.setup().press(screen.getByRole("button", { name: "Ouvrir l’audio 2" }));
    await screen.findByRole("button", { name: "Lire l’audio" });
    expect(screen.queryByRole("image")).not.toBeOnTheScreen(); expect(mockDispose).toHaveBeenCalledTimes(1);
  });
  it("stops playback and removes the cache on route blur even when the screen remains mounted", async () => {
    jest.mocked(getGoalMediaArtifacts).mockResolvedValue([audio.artifact]);
    await render(<GoalMediaArtifacts {...props} />);
    await userEvent.setup().press(await screen.findByRole("button", { name: "Lire l’audio" }));
    expect(mockPlayer.play).toHaveBeenCalledTimes(1);
    await act(async () => mockBlur?.());
    expect(mockPlayer.pause).toHaveBeenCalled(); expect(mockDispose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Lire l’audio" })).not.toBeOnTheScreen();
    await screen.rerender(<GoalMediaArtifacts {...props} />);
    expect(getGoalMediaArtifacts).toHaveBeenCalledTimes(1);
  });
  it.each(["hidden", "offline", "background", "blur", "pairing", "unmount"])("aborts pending bytes and creates no cache after %s", async (reason) => {
    const waiting = deferred<Uint8Array>(); jest.mocked(getGoalMediaBytes).mockReturnValue(waiting.promise);
    await render(<GoalMediaArtifacts {...props} />); await screen.findByText("Chargement du média…");
    const [, accepts, signal] = jest.mocked(getGoalMediaBytes).mock.calls[0];
    if (reason === "hidden") await screen.rerender(<GoalMediaArtifacts {...props} visible={false} />);
    if (reason === "offline") await screen.rerender(<GoalMediaArtifacts {...props} enabled={false} />);
    if (reason === "background") await act(async () => background("background"));
    if (reason === "blur") await act(async () => mockBlur?.());
    if (reason === "pairing") await act(async () => notifyConnectionChanged());
    if (reason === "unmount") await screen.unmount();
    expect(accepts?.()).toBe(false); expect(signal?.aborted).toBe(true);
    await act(async () => waiting.resolve(image.bytes)); expect(createGoalMediaCache).not.toHaveBeenCalled();
  });
  it("discards an old metadata request on changed goal identity", async () => {
    const waiting = deferred<typeof image.artifact[]>(); jest.mocked(getGoalMediaArtifacts).mockReturnValueOnce(waiting.promise);
    await render(<GoalMediaArtifacts {...props} />);
    const accepts = jest.mocked(getGoalMediaArtifacts).mock.calls[0][1];
    await screen.rerender(<GoalMediaArtifacts {...props} goalId={`goal_${"e".repeat(32)}`} nodes={[]} />);
    await act(async () => waiting.resolve([image.artifact]));
    expect(accepts?.()).toBe(false); expect(getGoalMediaBytes).not.toHaveBeenCalled();
  });
  it("shows a safe retry after download failure, never a raw transport error", async () => {
    jest.mocked(getGoalMediaBytes).mockRejectedValueOnce(new Error("private token detail"));
    await render(<GoalMediaArtifacts {...props} />);
    await userEvent.setup().press(await screen.findByRole("button", { name: "Réessayer ce média" }));
    await screen.findByRole("image");
    expect(screen.queryByText("private token detail")).not.toBeOnTheScreen();
  });
  it("does not display unrelated job artifacts or issue downloads for them", async () => {
    jest.mocked(getGoalMediaArtifacts).mockResolvedValue([{ ...image.artifact, job_id: `job_${"e".repeat(32)}` }]);
    await render(<GoalMediaArtifacts {...props} />);
    await screen.findByText("Aucun média validé n’est disponible pour ces étapes.");
    expect(getGoalMediaBytes).not.toHaveBeenCalled();
  });
});
