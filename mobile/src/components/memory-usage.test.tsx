import { act, render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";
import { AppState } from "react-native";
import { MemoryUsage } from "@/components/memory-usage";
import { ApiError, getGoalMemoryUsage } from "@/lib/application-api/server";
import { notifyConnectionChanged } from "@/lib/connection-events";
import { memoryUsageEntry, memoryUsageItem, memoryUsagePage } from "@/testing/memory-usage-fixtures";
jest.mock("@/lib/application-api/server", () => ({ getGoalMemoryUsage: jest.fn(),
  ApiError: class extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status; } } }));
const load = jest.mocked(getGoalMemoryUsage);
const props = { goalId: "goal_1", enabled: true };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(next => { resolve = next; }); return { resolve, promise }; }
async function open() { const user = userEvent.setup(); await render(<MemoryUsage {...props} />); await user.press(screen.getByRole("button", { name: "Mémoire utilisée" })); return user; }
describe("passive memory receipts", () => {
  beforeEach(() => { jest.restoreAllMocks(); jest.clearAllMocks(); load.mockReset(); AppState.currentState = "active"; load.mockResolvedValue(memoryUsagePage()); });
  it("loads only on demand, distinguishes historical proof and hides diagnostics", async () => {
    load.mockResolvedValue(memoryUsagePage([memoryUsageEntry({ items: [memoryUsageItem({ summary: "Longue exigence. ".repeat(40) }), memoryUsageItem({ id: "pmem_1", scope: "project", source_state: "missing", summary: null })], omitted_item_count: 2 })]));
    const user = userEvent.setup(); await render(<MemoryUsage {...props} />); expect(load).not.toHaveBeenCalled();
    await user.press(screen.getByRole("button", { name: "Mémoire utilisée" }));
    expect(await screen.findByText("Joints à un appel enregistré")).toBeOnTheScreen();
    expect(screen.getByText(/pas que le modèle a lu, compris/)).toBeOnTheScreen();
    expect(screen.getByText("Révision actuelle des consignes : 2")).toBeOnTheScreen();
    expect(screen.getByText("Révision des consignes dans ce reçu : 1")).toBeOnTheScreen();
    expect(screen.getByText(/Classement : non enregistré/)).toBeOnTheScreen();
    expect(screen.getByText(/Version de source non confirmée/)).toBeOnTheScreen();
    expect(screen.getByText(/2 références omises/)).toBeOnTheScreen();
    expect(screen.queryByText("Modèle : local-7b")).not.toBeOnTheScreen();
    expect(screen.queryByText("Longue exigence. ".repeat(40))).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Extrait mémoire mem_1" }));
    expect(screen.getByText("Longue exigence. ".repeat(40))).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Diagnostic du reçu model_call:gmc_1" }));
    expect(screen.getByText("Modèle : local-7b")).toBeOnTheScreen();
    expect(screen.getByText("État enregistré : failed")).toBeOnTheScreen();
    expect(load).toHaveBeenCalledTimes(1); expect(load).toHaveBeenCalledWith("goal_1", undefined, expect.any(Function));
  });
  it("retains evidence honestly on failure, resets pagination and never treats unavailable as empty", async () => {
    load.mockResolvedValueOnce(memoryUsagePage(undefined, { next_cursor: "page_two" })).mockRejectedValueOnce(new ApiError(400, "stale"))
      .mockResolvedValueOnce(memoryUsagePage([], { project_id: null }));
    const user = await open(); await user.press(await screen.findByRole("button", { name: "Voir les reçus précédents" }));
    expect(await screen.findByText(/L’historique a changé/)).toBeOnTheScreen();
    expect(screen.getByText("Préserver les horaires confirmés.")).toBeOnTheScreen();
    expect(screen.getByText(/Dernier relevé conservé/)).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Voir les reçus précédents" })).toBeDisabled();
    await user.press(screen.getByRole("button", { name: "Actualiser les reçus mémoire" }));
    expect(await screen.findByText(/Cela ne prouve pas qu’aucune mémoire/)).toBeOnTheScreen();
    expect(screen.getByText(/Aucun rattachement de projet/)).toBeOnTheScreen();
    expect(screen.queryByText("Préserver les horaires confirmés.")).not.toBeOnTheScreen();
    expect(load.mock.calls.map(call => call[1])).toEqual([undefined, "page_two", undefined]);
  });
  it("deduplicates loaded receipts across pages and resets on explicit refresh", async () => {
    const old = memoryUsageEntry({ id: "retrieval:q_old", evidence_stage: "retrieved", model_call_id: null, context_id: null,
      items: [memoryUsageItem({ summary: "Ancienne citation" })], retrieval: { mode: "semantic", reason: null, provider_fingerprint: null } });
    load.mockResolvedValueOnce(memoryUsagePage(undefined, { next_cursor: "page_two" })).mockResolvedValueOnce(memoryUsagePage([memoryUsageEntry(), old]))
      .mockResolvedValueOnce(memoryUsagePage());
    const user = await open(); await user.press(await screen.findByRole("button", { name: "Voir les reçus précédents" }));
    expect(await screen.findByText("Ancienne citation")).toBeOnTheScreen();
    expect(screen.getAllByText("Préserver les horaires confirmés.")).toHaveLength(1);
    expect(screen.getByText(/Classement : sémantique/)).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Actualiser les reçus mémoire" }));
    expect(screen.queryByText("Ancienne citation")).not.toBeOnTheScreen();
  });
  it.each(["pairing", "background", "disabled", "project", "closed"] as const)("discards pending receipts after %s", async reason => {
    const pending = deferred<ReturnType<typeof memoryUsagePage>>(); load.mockReturnValueOnce(pending.promise);
    let stateListener!: (state: "active" | "background") => void;
    jest.spyOn(AppState, "addEventListener").mockImplementation((_, callback) => { stateListener = callback; return { remove: jest.fn() }; });
    const user = await open(); const fence = load.mock.calls[0][2]!;
    if (reason === "pairing") await act(async () => notifyConnectionChanged());
    if (reason === "background") await act(async () => stateListener("background"));
    if (reason === "disabled") await screen.rerender(<MemoryUsage {...props} enabled={false} />);
    if (reason === "project") { load.mockResolvedValue(memoryUsagePage([], { goal_id: "goal_other" })); await screen.rerender(<MemoryUsage {...props} goalId="goal_other" />); }
    if (reason === "closed") await user.press(screen.getByRole("button", { name: "Mémoire utilisée" }));
    expect(fence()).toBe(false);
    await act(async () => pending.resolve(memoryUsagePage()));
    expect(screen.queryByText("Préserver les horaires confirmés.")).not.toBeOnTheScreen();
    if (reason === "pairing") { expect(screen.getByText(/Le jumelage a changé/)).toBeOnTheScreen(); expect(load).toHaveBeenCalledTimes(1); }
    if (reason === "background") { await act(async () => stateListener("active")); expect(load).toHaveBeenCalledTimes(1); }
  });
  it("clears already displayed excerpts on connection change and requires explicit refresh", async () => {
    const user = await open(); expect(await screen.findByText("Préserver les horaires confirmés.")).toBeOnTheScreen();
    await act(async () => notifyConnectionChanged());
    expect(screen.queryByText("Préserver les horaires confirmés.")).not.toBeOnTheScreen(); expect(load).toHaveBeenCalledTimes(1);
    load.mockResolvedValue(memoryUsagePage([], { project_id: null }));
    await user.press(screen.getByRole("button", { name: "Actualiser les reçus mémoire" }));
    expect(await screen.findByText(/Aucun reçu mémoire consultable/)).toBeOnTheScreen();
  });
});
