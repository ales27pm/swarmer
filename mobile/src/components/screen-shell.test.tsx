import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { afterEach, beforeEach, describe, expect, it, jest } from "@jest/globals";
import { useState } from "react";
import { Keyboard, ScrollView, Text, TextInput, View, type KeyboardEvent } from "react-native";

import { KeyboardInputGroup, KeyboardTextInput, ScreenShell } from "@/components/screen-shell";
import { ActionButton } from "@/components/swarm-ui";

const keyboardEvent = {
  duration: 250,
  easing: "keyboard",
  endCoordinates: { screenX: 0, screenY: 566, width: 393, height: 286 },
} as const;
const save = jest.fn();
let groupTop: number;
let groupHeight: number;
let inputTop: number;
let nextFrame: FrameRequestCallback | undefined;
let measureGroup: ((callback: (x: number, y: number, width: number, height: number) => void) => void) | undefined;
const keyboardListeners = new Map<string, (event: KeyboardEvent) => void>();

function Composer({ numeric = false }: { numeric?: boolean }) {
  const [value, setValue] = useState(numeric ? "256" : "");
  return (
    <ScreenShell title="Projet" testID="test-scroll">
      <Text>Historique du projet qui ne doit pas être révélé avec le composeur.</Text>
      <KeyboardInputGroup dismissKeyboard={numeric} testID="test-composer">
        <KeyboardTextInput
          accessibilityLabel="Brouillon"
          value={value}
          onChangeText={setValue}
          multiline={!numeric}
          keyboardType={numeric ? "number-pad" : "default"}
        />
        <ActionButton label="Enregistrer" onPress={save} />
      </KeyboardInputGroup>
    </ScreenShell>
  );
}

async function runFrame() {
  await act(() => {
    const callback = nextFrame;
    nextFrame = undefined;
    callback?.(0);
  });
}

beforeEach(() => {
  groupTop = 500;
  groupHeight = 200;
  inputTop = 200;
  nextFrame = undefined;
  measureGroup = undefined;
  keyboardListeners.clear();
  save.mockClear();
  jest.spyOn(globalThis, "requestAnimationFrame").mockImplementation((callback) => {
    nextFrame = callback;
    return 1;
  });
  jest.spyOn(globalThis, "cancelAnimationFrame").mockImplementation(() => { nextFrame = undefined; });
  jest.spyOn(Keyboard, "metrics").mockReturnValue(keyboardEvent.endCoordinates);
  jest.spyOn(Keyboard, "addListener").mockImplementation((name, listener) => {
    keyboardListeners.set(name, listener);
    return { remove: () => { keyboardListeners.delete(name); } } as ReturnType<typeof Keyboard.addListener>;
  });
  jest.spyOn(ScrollView.prototype, "getNativeScrollRef").mockReturnValue({
    measureInWindow: (callback) => callback(0, 100, 393, 750),
  } as NonNullable<ReturnType<ScrollView["getNativeScrollRef"]>>);
  jest.spyOn(View.prototype, "measureInWindow").mockImplementation(function (this: View | TextInput, callback) {
    if (this instanceof TextInput) callback(0, inputTop, 393, 104);
    else if (measureGroup) measureGroup(callback);
    else callback(0, groupTop, 393, groupHeight);
  });
  jest.spyOn(ScrollView.prototype, "scrollTo").mockClear();
});
afterEach(() => { jest.restoreAllMocks(); });

describe("ScreenShell", () => {
  it("renders when keyboard metrics are unavailable on the web", async () => {
    const metrics = Keyboard.metrics;
    Object.defineProperty(Keyboard, "metrics", { value: undefined, configurable: true });
    try {
      await render(<ScreenShell title="Aperçu web"><Text>Prêt</Text></ScreenShell>);
      expect(screen.getByText("Prêt")).toBeOnTheScreen();
    } finally { Object.defineProperty(Keyboard, "metrics", { value: metrics, configurable: true }); }
  });

  it("keeps a long objective accessible when its heading is collapsed", async () => {
    const title = "Objectif détaillé ".repeat(20);
    await render(<ScreenShell title={title} />);
    expect(screen.getByText(title)).toHaveProp("numberOfLines", 3);
    await fireEvent.press(screen.getByRole("button", { name: "Lire l’objectif complet" }));
    expect(screen.getByText(title)).not.toHaveProp("numberOfLines", 3);
  });

  it("does not truncate a multiline heading without offering expansion", async () => {
    const title = "Préparer agenda\nLire le calendrier\nComparer disponibilités\nProposer créneaux";
    await render(<ScreenShell title={title} />);
    expect(screen.getByText(title)).not.toHaveProp("numberOfLines", 3);
    expect(screen.queryByRole("button", { name: "Lire l’objectif complet" })).not.toBeOnTheScreen();
  });

  it("declares French for assistive-technology pronunciation", async () => {
    await render(
      <ScreenShell title="Réglages" testID="screen-shell">
        <Text>Connexion authentifiée</Text>
      </ScreenShell>,
    );

    expect(screen.getByTestId("screen-shell")).toHaveProp(
      "accessibilityLanguage",
      "fr-FR",
    );
  });

  it("reveals the focused composer below a navigation header and follows multiline growth without changing the draft", async () => {
    await render(<Composer />);
    const input = screen.getByLabelText("Brouillon");
    const draft = "Première ligne\nDeuxième ligne\nTroisième ligne\nQuatrième ligne\nCinquième ligne\nSixième ligne";
    await fireEvent.scroll(screen.getByTestId("test-scroll"), { nativeEvent: { contentOffset: { y: 200 } } });
    await fireEvent(input, "focus");
    await fireEvent.changeText(input, draft);
    await act(() => keyboardListeners.get("keyboardDidShow")?.(keyboardEvent));
    await runFrame();
    // Its bottom starts at 700; moving 146 points leaves 12 points above the keyboard at 566.
    expect(ScrollView.prototype.scrollTo).toHaveBeenLastCalledWith({ y: 346, animated: false });

    groupTop = 354;
    groupHeight = 230;
    await fireEvent.scroll(screen.getByTestId("test-scroll"), { nativeEvent: { contentOffset: { y: 346 } } });
    await fireEvent(input, "contentSizeChange", { nativeEvent: { contentSize: { width: 393, height: 230 } } });
    await runFrame();
    expect(ScrollView.prototype.scrollTo).toHaveBeenLastCalledWith({ y: 376, animated: false });
    expect(input).toHaveProp("value", draft);
    expect(input).toHaveProp("multiline", true);
    expect(save).not.toHaveBeenCalled();

    groupTop = 324;
    await fireEvent(input, "selectionChange", { nativeEvent: { selection: { start: 0, end: 0 } } });
    await runFrame();
    expect(ScrollView.prototype.scrollTo).toHaveBeenCalledTimes(2);
  });

  it("rechecks the focused group when an error above it changes the scroll content", async () => {
    groupTop = 300;
    await render(<Composer numeric />);
    await fireEvent(screen.getByLabelText("Brouillon"), "focus");
    await runFrame();
    expect(ScrollView.prototype.scrollTo).not.toHaveBeenCalled();

    groupTop = 480;
    await fireEvent(screen.getByTestId("test-scroll"), "contentSizeChange", 393, 1500);
    await runFrame();
    expect(ScrollView.prototype.scrollTo).toHaveBeenCalledWith({ y: 126, animated: false });
  });

  it("dismisses the numeric keyboard explicitly without saving or changing the value", async () => {
    const dismiss = jest.spyOn(Keyboard, "dismiss").mockImplementation(() => undefined);
    await render(<Composer numeric />);
    expect(screen.queryByRole("button", { name: "Fermer le clavier" })).not.toBeOnTheScreen();
    await fireEvent(screen.getByLabelText("Brouillon"), "focus");
    await fireEvent.press(screen.getByRole("button", { name: "Fermer le clavier" }));
    expect(dismiss).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Brouillon")).toHaveProp("value", "256");
    expect(save).not.toHaveBeenCalled();
  });

  it("keeps the active input visible without oscillating when its group cannot fit above the keyboard", async () => {
    groupTop = 108;
    groupHeight = 500;
    await render(<Composer />);
    const input = screen.getByLabelText("Brouillon");
    await fireEvent(input, "focus");
    await runFrame();
    expect(ScrollView.prototype.scrollTo).not.toHaveBeenCalled();
    groupTop = 54;
    await fireEvent(input, "selectionChange", { nativeEvent: { selection: { start: 0, end: 0 } } });
    await runFrame();
    expect(ScrollView.prototype.scrollTo).not.toHaveBeenCalled();

    inputTop = 600;
    await fireEvent(input, "contentSizeChange", { nativeEvent: { contentSize: { width: 393, height: 104 } } });
    await runFrame();
    expect(ScrollView.prototype.scrollTo).toHaveBeenCalledWith({ y: 150, animated: false });
  });

  it("uses the latest keyboard frame even while Keyboard.metrics still reports the previous frame", async () => {
    groupTop = 400;
    groupHeight = 100;
    await render(<Composer />);
    await fireEvent(screen.getByLabelText("Brouillon"), "focus");
    await runFrame();
    expect(ScrollView.prototype.scrollTo).not.toHaveBeenCalled();
    await act(() => keyboardListeners.get("keyboardDidChangeFrame")?.({
      ...keyboardEvent,
      endCoordinates: { ...keyboardEvent.endCoordinates, screenY: 450 },
    }));
    await runFrame();
    expect(Keyboard.metrics()?.screenY).toBe(566);
    expect(ScrollView.prototype.scrollTo).toHaveBeenCalledWith({ y: 62, animated: false });
  });

  it("does not reveal after keyboard hiding starts even if layout and selection events continue", async () => {
    await render(<Composer />);
    const input = screen.getByLabelText("Brouillon");
    await fireEvent(input, "focus");
    await act(() => keyboardListeners.get("keyboardWillHide")?.(keyboardEvent));
    await act(() => keyboardListeners.get("keyboardDidChangeFrame")?.(keyboardEvent));
    await fireEvent(input, "selectionChange", { nativeEvent: { selection: { start: 0, end: 0 } } });
    await fireEvent(screen.getByTestId("test-scroll"), "contentSizeChange", 393, 1600);
    await runFrame();
    expect(ScrollView.prototype.scrollTo).not.toHaveBeenCalled();
  });

  it.each(["blur", "unmount", "keyboard hide"])("ignores a delayed native measurement after %s", async (event) => {
    let complete!: (x: number, y: number, width: number, height: number) => void;
    measureGroup = (callback) => { complete = callback; };
    const view = await render(<Composer />);
    await fireEvent(screen.getByLabelText("Brouillon"), "focus");
    await runFrame();
    if (event === "blur") await fireEvent(screen.getByLabelText("Brouillon"), "blur");
    else if (event === "unmount") await view.unmount();
    else await act(() => keyboardListeners.get("keyboardWillHide")?.(keyboardEvent));
    await act(() => complete(0, 500, 393, 200));
    expect(ScrollView.prototype.scrollTo).not.toHaveBeenCalled();
  });
});
