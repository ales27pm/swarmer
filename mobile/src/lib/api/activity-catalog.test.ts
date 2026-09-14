import { describe, expect, it } from "@jest/globals";
import { catalogSearchText, parseActivityCatalog } from "@/lib/api/activity-catalog";
import { activityCatalogFixture as fixture } from "@/test-fixtures/activity-catalog";

describe("activity catalogue boundary", () => {
  it("retains distinct worker, iPhone and future integration states", () => {
    expect(parseActivityCatalog(fixture)).toEqual(fixture);
    expect(catalogSearchText("Échéances PERSONNELLES")).toBe("echeances personnelles");
  });

  it.each([
    { state: "goal_ready", agent_ids: ["agent"] },
    { state: "iphone_request", agent_ids: [] },
  ])("never accepts a future integration as executable: %j", (availability) => {
    const value = structuredClone(fixture);
    Object.assign(value.skills[2].availability, availability);
    expect(() => parseActivityCatalog(value)).toThrow("catalogue reçu");
  });

  it("requires a matching worker for a ready skill", () => {
    const value = structuredClone(fixture);
    value.skills[0].availability.agent_ids = [];
    expect(() => parseActivityCatalog(value)).toThrow("catalogue reçu");
  });

  it("rejects dangling role references and duplicate identifiers", () => {
    const dangling = structuredClone(fixture);
    dangling.roles[0].skill_ids.push("missing");
    expect(() => parseActivityCatalog(dangling)).toThrow("catalogue reçu");
    expect(() => parseActivityCatalog({ ...fixture, domains: [...fixture.domains, fixture.domains[0]] })).toThrow("catalogue reçu");
  });

  it("rejects an unknown response or availability version", () => {
    expect(() => parseActivityCatalog({ ...fixture, schema_version: "2.0" })).toThrow("catalogue reçu");
    const value = structuredClone(fixture);
    Object.assign(value.skills[0].availability, { state: "enabled_by_catalogue" });
    expect(() => parseActivityCatalog(value)).toThrow("catalogue reçu");
  });
});
