import { render, screen, userEvent, waitFor } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import AgentsScreen from "@/../app/(main)/agents";
import { listAgents } from "@/lib/api/client";

jest.mock("@/lib/api/client", () => ({ listAgents: jest.fn() }));

const mockListAgents = jest.mocked(listAgents);

describe("AgentsScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("provides a visible heartbeat refresh action", async () => {
    mockListAgents.mockResolvedValue([]);
    const user = userEvent.setup();
    await render(<AgentsScreen />);

    await screen.findByText("Aucun agent enregistré");
    await user.press(screen.getByRole("button", { name: "Actualiser les heartbeats" }));
    await waitFor(() => expect(mockListAgents).toHaveBeenCalledTimes(2));
  });

  it("does not claim the agent registry is empty when loading fails", async () => {
    mockListAgents.mockRejectedValue(new Error("Agents indisponibles"));
    await render(<AgentsScreen />);

    expect(await screen.findByText("Agents indisponibles")).toBeOnTheScreen();
    expect(screen.queryByText("Aucun agent enregistré")).not.toBeOnTheScreen();
  });

  it("counts a busy authenticated agent as active", async () => {
    mockListAgents.mockResolvedValue([
      {
        id: "agent_busy",
        name: "Code worker",
        version: "1.0.0",
        endpoint: "local://workers/code",
        model_id: null,
        status: "busy",
        skills: ["code_review.git_status"],
        last_heartbeat_at: "2026-09-04T12:00:00Z",
        last_seen_at: "2026-09-04T12:00:00Z",
        max_concurrency: 1,
        capacity: {},
        runtime: "python",
        supported_protocol_version: "mongars-worker-v0.9",
        active_jobs: 1,
        historical_score: 0.5,
        agent_card: {
          agent_id: "agent_busy",
          name: "Code worker",
          version: "1.0.0",
          skills: ["code_review.git_status"],
          model_id: null,
          runtime: "python",
          max_concurrency: 1,
          supported_protocol_version: "mongars-worker-v0.9",
          capabilities: {},
        },
        created_at: "2026-09-04T12:00:00Z",
        updated_at: "2026-09-04T12:00:00Z",
      },
    ]);
    await render(<AgentsScreen />);

    expect(await screen.findByText(/1\/1 agent actif/)).toBeOnTheScreen();
  });
});
