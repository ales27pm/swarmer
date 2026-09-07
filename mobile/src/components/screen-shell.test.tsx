import { render, screen } from "@testing-library/react-native";
import { describe, expect, it } from "@jest/globals";
import { Text } from "react-native";

import { ScreenShell } from "@/components/screen-shell";

describe("ScreenShell", () => {
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
});
