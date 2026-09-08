import { describe, expect, it, jest } from "@jest/globals";

import {
  buildLocalProposalPrompt,
  isActionableToolProposal,
  isHuggingFaceModelId,
  isImmutableHuggingFaceRevision,
  parseLocalToolProposal,
} from "@/lib/local-inference";

jest.mock("expo", () => ({ requireOptionalNativeModule: jest.fn(() => null) }));

describe("local inference proposal boundary", () => {
  it("accepts an exact actionable proposal without claiming execution", () => {
    const proposal = parseLocalToolProposal(
      JSON.stringify({
        tool_name: "process.run",
        arguments: {
          argv: ["npm", "test"],
          cwd: ".",
          timeout_seconds: 30,
        },
        summary: "Run the test suite",
      }),
    );

    expect(isActionableToolProposal(proposal)).toBe(true);
    expect(proposal).toEqual({
      tool_name: "process.run",
      arguments: {
        argv: ["npm", "test"],
        cwd: ".",
        timeout_seconds: 30,
      },
      summary: "Run the test suite",
    });
  });

  it("keeps a no-tool response inert and trims its visible summary", () => {
    const proposal = parseLocalToolProposal(
      '{"tool_name":"none","arguments":{},"summary":"  Réponse locale seulement  "}',
    );

    expect(isActionableToolProposal(proposal)).toBe(false);
    expect(proposal).toEqual({
      tool_name: "none",
      arguments: {},
      summary: "Réponse locale seulement",
    });
  });

  it.each([
    [
      "surrounding prose",
      'Voici le résultat: {"tool_name":"none","arguments":{},"summary":"texte"}',
    ],
    [
      "markdown fences",
      '```json\n{"tool_name":"none","arguments":{},"summary":"texte"}\n```',
    ],
    [
      "an unknown top-level field",
      '{"tool_name":"none","arguments":{},"summary":"texte","completed":true}',
    ],
    [
      "arguments attached to none",
      '{"tool_name":"none","arguments":{"path":"."},"summary":"texte"}',
    ],
    [
      "an unknown tool argument",
      '{"tool_name":"workspace.read_text","arguments":{"path":"README.md","secret":true},"summary":"Read"}',
    ],
    [
      "an empty process command",
      '{"tool_name":"process.run","arguments":{"argv":[]},"summary":"Run"}',
    ],
    [
      "a simulated completion field",
      '{"tool_name":"workspace.list_dir","arguments":{"path":"."},"summary":"Done","status":"completed"}',
    ],
    [
      "a duplicate top-level key",
      '{"tool_name":"none","tool_name":"workspace.list_dir","arguments":{"path":"."},"summary":"List"}',
    ],
    [
      "an escaped duplicate top-level key",
      '{"tool_name":"none","tool\\u005fname":"none","arguments":{},"summary":"Text"}',
    ],
    [
      "a duplicate nested argument",
      '{"tool_name":"workspace.read_text","arguments":{"path":"README.md","path":"secret.txt"},"summary":"Read"}',
    ],
  ])("rejects %s", (_label, text) => {
    expect(() => parseLocalToolProposal(text)).toThrow();
  });

  it("requires a full immutable Hugging Face commit", () => {
    expect(isHuggingFaceModelId("mlx-community/model-4bit")).toBe(true);
    expect(isHuggingFaceModelId("https://huggingface.co/org/model")).toBe(false);
    expect(isHuggingFaceModelId("model-only")).toBe(false);
    expect(isImmutableHuggingFaceRevision("a".repeat(40))).toBe(true);
    expect(isImmutableHuggingFaceRevision("main")).toBe(false);
    expect(isImmutableHuggingFaceRevision("v1.0.0")).toBe(false);
    expect(isImmutableHuggingFaceRevision("a".repeat(39))).toBe(false);
  });

  it("builds a proposal-only prompt without execution authority", () => {
    const prompt = buildLocalProposalPrompt("Inspecte le dépôt");

    expect(prompt).toContain("proposes seulement");
    expect(prompt).toContain("Ne prétends jamais qu’elle a été exécutée");
    expect(prompt).toContain("Intention: Inspecte le dépôt");
  });
});
