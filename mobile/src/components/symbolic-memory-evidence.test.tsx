import { render, screen, userEvent } from "@testing-library/react-native";
import { describe, expect, it } from "@jest/globals";
import ordinary from "@/testing/symbolic-http-ordinary.json";
import { SymbolicMemoryEvidence } from "./symbolic-memory-evidence";
import type { MemoryItem } from "@/lib/api/types";

describe("symbolic evidence display", () => {
  it("labels proposals as unvalidated and expands their complete conditions and sources", async () => {
    const item = ordinary[0] as unknown as MemoryItem;
    await render(<SymbolicMemoryEvidence item={item} />);
    expect(screen.getByText("Proposition non validée")).toBeOnTheScreen();
    const card = item.symbolic_evidence![0]; const id = card.proposal.proposal_id;
    expect(screen.queryByTestId(`symbolic-content-${id}`)).not.toBeOnTheScreen();
    await userEvent.setup().press(screen.getByTestId(`symbolic-details-${id}`));
    const displayed = screen.getByTestId(`symbolic-content-${id}`).props.children as string;
    expect(JSON.parse(displayed)).toEqual({ proposal: card.proposal, concepts: card.concepts, relations: card.relations, matches: card.matches });
    expect(displayed).toContain("only_after"); expect(displayed).toContain("Archive.py");
    expect(displayed).toContain(card.proposal.sources[0].binding.document_sha256);
  });

  it("does not display incomplete or malformed evidence as a valid proposal", async () => {
    const item = { ...ordinary[0], symbolic_evidence: null } as unknown as MemoryItem;
    await render(<SymbolicMemoryEvidence item={item} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/indisponibles/);
    expect(screen.queryByText("Proposition non validée")).not.toBeOnTheScreen();
  });
});
