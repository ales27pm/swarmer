import { render, screen, userEvent } from "@testing-library/react-native";
import { describe, expect, it } from "@jest/globals";
import { RecordedFailure } from "./recorded-failure";

describe("RecordedFailure", () => {
  it.each([
    ["writing_needs_clarification", "Précision demandée"],
    ["writing_insufficient_sources", "Sources insuffisantes"],
    ["writing_requirements_unmet", "Document non conforme"],
    ["document_requirement_unverifiable", "Conformité du document non établie"],
    ["writing_evidence_invalid", "Preuve de rédaction invalide"],
    ["writing_budget_exceeded", "Limite de rédaction atteinte"],
    ["model_declined", "Refus déclaré par le modèle"],
  ])("presents %s as an actionable outcome with optional diagnostics", async (reason, label) => {
    await render(<RecordedFailure reason={reason} />);
    expect(screen.getByText(label)).toBeOnTheScreen();
    expect(screen.queryByText(`Code enregistré : ${reason}`)).not.toBeOnTheScreen();
    await userEvent.setup().press(screen.getByRole("button", { name: "Voir le diagnostic" }));
    expect(screen.getByText(`Code enregistré : ${reason}`)).toHaveProp("selectable", true);
  });

  it.each(["I'm sorry, could you clarify the preferred format?", "constructor", "__proto__"])("does not classify unrecognized content %s as a refusal", async (reason) => {
    await render(<RecordedFailure reason={reason} />);
    expect(screen.getByText(reason)).toBeOnTheScreen();
    expect(screen.queryByText("Refus déclaré par le modèle")).not.toBeOnTheScreen();
  });
});
