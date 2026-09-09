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
  return { Tabs };
});

describe("MainLayout", () => {
  beforeEach(() => {
    mockTabScreen.mockClear();
  });

  it("promotes Swarm to the main navigation while retaining the agent registry route", async () => {
    await render(<MainLayout />);

    const screens = new Map(mockTabScreen.mock.calls.map(([props]) => [props.name, props]));
    expect(screens.get("swarm")).toMatchObject({ options: { title: "Swarm" } });
    expect(screens.get("agents")).toMatchObject({ options: { href: null, title: "Agents" } });
    expect(screens.get("settings")).toMatchObject({ options: { href: null } });
  });
});
