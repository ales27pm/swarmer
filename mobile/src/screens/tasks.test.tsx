import { render, screen } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import TasksScreen from "@/../app/(main)/tasks";
import { listTasks } from "@/lib/api/client";

const mockPush = jest.fn();

jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));
jest.mock("@/lib/api/client", () => ({ listTasks: jest.fn() }));

const mockListTasks = jest.mocked(listTasks);

describe("TasksScreen", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("keeps every lifecycle filter visible without a hidden horizontal tail", async () => {
    mockListTasks.mockResolvedValue([]);
    await render(<TasksScreen />);

    expect(await screen.findByRole("button", { name: "Toutes" })).toBeOnTheScreen();
    expect(screen.getByRole("button", { name: "Annulées" })).toBeOnTheScreen();
  });

  it("does not claim the authoritative task list is empty when loading fails", async () => {
    mockListTasks.mockRejectedValue(new Error("Tâches indisponibles"));
    await render(<TasksScreen />);

    expect(await screen.findByText("Tâches indisponibles")).toBeOnTheScreen();
    expect(screen.queryByText("Aucune tâche")).not.toBeOnTheScreen();
  });
});
