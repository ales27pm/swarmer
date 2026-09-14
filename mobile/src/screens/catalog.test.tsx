import { render, screen, userEvent } from "@testing-library/react-native";
import { beforeEach, describe, expect, it, jest } from "@jest/globals";

import CatalogScreen from "@/../app/(main)/catalog";
import { getActivityCatalog } from "@/lib/api/client";
import { activityCatalogFixture } from "@/test-fixtures/activity-catalog";

const mockPush = jest.fn();
jest.mock("expo-router", () => ({ useRouter: () => ({ push: mockPush }) }));
jest.mock("@/lib/api/client", () => ({ getActivityCatalog: jest.fn() }));
const getCatalog = jest.mocked(getActivityCatalog);

describe("agent activity catalogue", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    getCatalog.mockResolvedValue({ ...structuredClone(activityCatalogFixture), generated_at: new Date().toISOString() });
  });

  it("searches personal activities and exposes required integrations", async () => {
    const user = userEvent.setup();
    await render(<CatalogScreen />);
    await screen.findByText("2 domaines · 2 profils");
    await user.type(screen.getByLabelText("Rechercher une activité"), "agenda");
    expect(screen.queryByTestId("catalog-role-developer")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Ouvrir la fiche Organisateur personnel" }));
    expect(screen.getByText("Accès iPhone intégré")).toBeOnTheScreen();
    expect(screen.getByText("À intégrer")).toBeOnTheScreen();
    expect(screen.getByText("Une demande explicite sur l’iPhone est nécessaire.")).toBeOnTheScreen();
    expect(screen.queryByRole("button", { name: /Lancer|Démarrer/ })).not.toBeOnTheScreen();
  });

  it("filters ready goals without advertising native or planned skills as ready", async () => {
    const user = userEvent.setup();
    await render(<CatalogScreen />);
    await screen.findByText("Développeur de projet");
    await user.press(screen.getByRole("button", { name: "Disponibles pour un but" }));
    expect(screen.getByTestId("catalog-role-developer")).toBeOnTheScreen();
    expect(screen.queryByTestId("catalog-role-organizer")).not.toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Voir les agents connectés" }));
    expect(mockPush).toHaveBeenCalledWith("/agents");
  });

  it("keeps the catalogue readable while invalidating availability after refresh failure", async () => {
    const user = userEvent.setup();
    await render(<CatalogScreen />);
    await screen.findByText("Développeur de projet");
    await user.press(screen.getByRole("button", { name: "Ouvrir la fiche Développeur de projet" }));
    expect(screen.getByText("Disponible pour un but")).toBeOnTheScreen();
    getCatalog.mockRejectedValue(new Error("Serveur indisponible"));
    await user.press(screen.getByRole("button", { name: "Actualiser le catalogue" }));
    expect(await screen.findByText("Serveur indisponible")).toBeOnTheScreen();
    expect(screen.queryByText("Disponible pour un but")).not.toBeOnTheScreen();
    expect(screen.getByText("Disponibilité à vérifier")).toBeOnTheScreen();
    expect(screen.getByText("Développeur de projet")).toBeOnTheScreen();
  });

  it("shows an initial fetch failure without inventing an empty catalogue", async () => {
    getCatalog.mockRejectedValue(new Error("Connexion requise"));
    await render(<CatalogScreen />);
    expect(await screen.findByText("Connexion requise")).toBeOnTheScreen();
    expect(screen.queryByText("Aucun profil pour ces critères")).not.toBeOnTheScreen();
  });

  it.each([-120_000, 60_000])("does not renew readiness from a stale or future server snapshot (%s ms)", async (offset) => {
    getCatalog.mockResolvedValue({ ...structuredClone(activityCatalogFixture), generated_at: new Date(Date.now() + offset).toISOString() });
    const user = userEvent.setup();
    await render(<CatalogScreen />);
    await screen.findByText("Développeur de projet");
    await user.press(screen.getByRole("button", { name: "Ouvrir la fiche Développeur de projet" }));
    expect(screen.queryByText("Disponible pour un but")).not.toBeOnTheScreen();
    expect(screen.getByText("Disponibilité à vérifier")).toBeOnTheScreen();
    await user.press(screen.getByRole("button", { name: "Disponibles pour un but" }));
    expect(screen.queryByTestId("catalog-role-developer")).not.toBeOnTheScreen();
  });
});
