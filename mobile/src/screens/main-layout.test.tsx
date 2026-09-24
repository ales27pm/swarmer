import { render } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import MainLayout from "@/../app/(main)/_layout";

type TabScreenProps = {
  name: string;
  options?: { href?: null; title?: string };
};

const mockTabScreen = jest.fn((_props: TabScreenProps) => null);

jest.mock("expo-router", () => {
  const React = jest.requireActual<typeof import("react")>("react");
  const Tabs = ({ children }: { children: React.ReactNode }) =>
    React.createElement(React.Fragment, null, children);
  function MockTabScreen(props: TabScreenProps) {
    mockTabScreen(props);
    return null;
  }
  Tabs.Screen = MockTabScreen;
  return { Tabs, useRouter: () => ({ replace: jest.fn() }) };
});

describe("MainLayout", () => {
  beforeEach(() => {
    mockTabScreen.mockClear();
  });

  it("groups the app in four visible sections while keeping secondary routes", async () => {
    await render(<MainLayout />);

    const screens = new Map(mockTabScreen.mock.calls.map(([props]) => [props.name, props]));
    const visible = [...screens.values()].filter((screen) => screen.options?.href !== null).map((screen) => screen.name);
    expect(visible).toEqual(["index", "tasks", "swarm", "settings"]);
    expect(screens.get("swarm")).toMatchObject({ options: { title: "Équipe" } });
    expect(screens.get("agents")).toMatchObject({ options: { href: null, title: "Agents connectés" } });
    expect(screens.get("catalog")).toMatchObject({ options: { href: null, title: "Compétences" } });
    expect(screens.get("approvals")).toMatchObject({ options: { href: null, title: "Autorisations" } });
    expect(screens.get("memory")).toMatchObject({ options: { href: null, title: "Mémoire" } });
  });
});
