import { render } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import RootLayout from "@/../app/_layout";

type StackScreenProps = {
  name: string;
  options?: { headerBackTitle?: string; title?: string };
};

const mockStackScreen = jest.fn((_props: StackScreenProps) => null);

jest.mock("@/lib/sync/live-sync-provider", () => ({
  LiveSyncProvider: ({ children }: { children: React.ReactNode }) => children,
}));

jest.mock("expo-router", () => {
  const React = jest.requireActual<typeof import("react")>("react");
  const Stack = ({ children }: { children: React.ReactNode }) =>
    React.createElement(React.Fragment, null, children);
  function MockStackScreen(props: StackScreenProps) {
    mockStackScreen(props);
    return null;
  }
  Stack.Screen = MockStackScreen;
  return { Stack };
});

describe("RootLayout", () => {
  beforeEach(() => {
    mockStackScreen.mockClear();
  });

  it("uses a user-facing back label for task details", async () => {
    await render(<RootLayout />);

    const taskScreen = mockStackScreen.mock.calls.find(
      ([props]) => props.name === "task/[id]",
    );
    expect(taskScreen?.[0]).toMatchObject({
      name: "task/[id]",
      options: { headerBackTitle: "Retour", title: "Tâche" },
    });
  });
});
