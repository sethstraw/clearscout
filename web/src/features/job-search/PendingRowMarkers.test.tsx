import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { PendingDiscovery, PendingMarkers } from "../../api/client";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { requestLog } from "../../test/requestLog";
import { pendingDiscoveryRow } from "./pendingDiscoveryFixtures";
import { PendingRowKeepNote, PendingRowMarkerToggles } from "./PendingRowMarkers";
import { usePendingRowMarkers } from "./usePendingRowMarkers";

const LABEL = "Analyst at Globex";

function installMarkers(item: PendingDiscovery) {
  const puts = requestLog();
  server.use(
    http.put(
      "*/api/v1/workspaces/:workspaceId/pending-discoveries/:pendingId/markers",
      async ({ request }) => {
        await puts.record(request);
        const body = puts.requests.at(-1)?.body as PendingMarkers;
        return HttpResponse.json({ ...item, ...body } satisfies PendingDiscovery);
      },
    ),
  );
  return puts;
}

function Markers({ item }: { item: PendingDiscovery }) {
  const markers = usePendingRowMarkers(item, LABEL, "workspace-1");
  return (
    <>
      <PendingRowMarkerToggles markers={markers} />
      <PendingRowKeepNote markers={markers} />
    </>
  );
}

function renderMarkers(item: PendingDiscovery) {
  render(
    <AppProviders>
      <Markers item={item} />
    </AppProviders>,
  );
}

const favoriteButton = () => screen.getByRole("button", { name: `Favorite ${LABEL}` });
const keepButton = () => screen.getByRole("button", { name: `Keep ${LABEL}` });
const noteInput = () => screen.getByLabelText<HTMLInputElement>(`Keep note for ${LABEL}`);
const shownNote = () =>
  screen.getByRole("button", { name: / Edit keep note for Analyst at Globex$/ });
// The note button is named by the note it shows, then by the row it edits.
const namedNote = (shown: string) =>
  screen.getByRole("button", { name: `${shown}. Edit keep note for ${LABEL}` });

describe("PendingRowMarkers", () => {
  it("names each toggle's state in its visible text and its pressed state", () => {
    renderMarkers(pendingDiscoveryRow({ favorite: true, kept: true }));

    expect(favoriteButton().textContent).toBe("Favorited");
    expect(favoriteButton().getAttribute("aria-pressed")).toBe("true");
    expect(keepButton().textContent).toBe("Kept");
    expect(keepButton().getAttribute("aria-pressed")).toBe("true");
  });

  it("names an unpressed toggle by the action it takes", () => {
    renderMarkers(pendingDiscoveryRow());

    expect(favoriteButton().textContent).toBe("Favorite");
    expect(favoriteButton().getAttribute("aria-pressed")).toBe("false");
    expect(keepButton().textContent).toBe("Keep");
  });

  it("favorites a row mid-edit without losing the click or the typed note", async () => {
    const item = pendingDiscoveryRow({ kept: true });
    const puts = installMarkers(item);
    renderMarkers(item);

    fireEvent.click(shownNote());
    const note = noteInput();
    fireEvent.change(note, { target: { value: "Recruiter called" } });
    // Pressing the button moves focus off the note before the click lands.
    act(() => favoriteButton().focus());
    fireEvent.click(favoriteButton());

    await waitFor(() => expect(puts.requests).toHaveLength(2));
    expect(puts.requests.at(-1)?.body).toEqual({
      favorite: true,
      kept: true,
      keep_note: "Recruiter called",
    });
    expect(namedNote("Kept: Recruiter called")).toBe(shownNote());
  });

  it("opens the note for editing in place and saves it once on Enter", async () => {
    const item = pendingDiscoveryRow({ kept: true, keep_note: "Strong team" });
    const puts = installMarkers(item);
    renderMarkers(item);

    expect(namedNote("Kept: Strong team")).toBe(shownNote());
    expect(screen.queryByLabelText(`Keep note for ${LABEL}`)).toBeNull();
    fireEvent.click(shownNote());
    expect(document.activeElement).toBe(noteInput());
    fireEvent.change(noteInput(), { target: { value: "Recruiter called" } });
    fireEvent.keyDown(noteInput(), { key: "Enter" });

    await waitFor(() => expect(puts.requests).toHaveLength(1));
    expect(puts.requests[0]?.body).toEqual({
      favorite: false,
      kept: true,
      keep_note: "Recruiter called",
    });
    expect(document.activeElement).toBe(shownNote());
  });

  it("cancels an edit on Escape, restoring the saved note without saving", async () => {
    const item = pendingDiscoveryRow({ kept: true, keep_note: "Strong team" });
    const puts = installMarkers(item);
    renderMarkers(item);

    fireEvent.click(shownNote());
    fireEvent.change(noteInput(), { target: { value: "Half-typed" } });
    fireEvent.keyDown(noteInput(), { key: "Escape" });

    expect(screen.queryByLabelText(`Keep note for ${LABEL}`)).toBeNull();
    expect(document.activeElement).toBe(namedNote("Kept: Strong team"));
    fireEvent.click(shownNote());
    expect(noteInput().value).toBe("Strong team");
    fireEvent.blur(noteInput());
    await waitFor(() => expect(screen.queryByLabelText(`Keep note for ${LABEL}`)).toBeNull());
    expect(puts.requests).toHaveLength(0);
  });

  it("invites a note on a kept row that has none", () => {
    renderMarkers(pendingDiscoveryRow({ kept: true }));
    expect(namedNote("Kept: add a note")).toBe(shownNote());
  });

  it("keeps the note focused through a mouse press, sending one request with the typed note", async () => {
    const item = pendingDiscoveryRow({ kept: true });
    const puts = installMarkers(item);
    renderMarkers(item);

    fireEvent.click(shownNote());
    const note = noteInput();
    fireEvent.change(note, { target: { value: "Recruiter called" } });
    const focusMoved = fireEvent.mouseDown(favoriteButton());
    fireEvent.click(favoriteButton());

    expect(focusMoved).toBe(false);
    expect(document.activeElement).toBe(note);
    await waitFor(() => expect(puts.requests).toHaveLength(1));
    expect(puts.requests[0]?.body).toEqual({
      favorite: true,
      kept: true,
      keep_note: "Recruiter called",
    });
  });

  it("asks before un-keeping a row with a note, and cancelling keeps both", async () => {
    const item = pendingDiscoveryRow({ kept: true, keep_note: "Strong team" });
    const puts = installMarkers(item);
    renderMarkers(item);

    fireEvent.click(keepButton());
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/This deletes the keep note\./)).toBeTruthy();
    fireEvent.click(dialog.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(puts.requests).toHaveLength(0);
    expect(namedNote("Kept: Strong team")).toBe(shownNote());

    fireEvent.click(keepButton());
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Remove keep marker",
      }),
    );
    await waitFor(() => expect(puts.requests).toHaveLength(1));
    expect(puts.requests[0]?.body).toEqual({ favorite: false, kept: false, keep_note: null });
  });

  it("warns that un-keeping a no-longer-listed row takes it out of the queue", async () => {
    const item = pendingDiscoveryRow({ kept: true, withdrawn_at: "2026-09-20T00:00:00Z" });
    installMarkers(item);
    renderMarkers(item);

    fireEvent.click(keepButton());

    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/leaves the pending queue/)).toBeTruthy();
    expect(dialog.queryByText(/deletes the keep note/)).toBeNull();
  });

  it("names the favorite when un-favoriting is what takes a row out of the queue", async () => {
    const item = pendingDiscoveryRow({ favorite: true, withdrawn_at: "2026-09-20T00:00:00Z" });
    installMarkers(item);
    renderMarkers(item);

    fireEvent.click(favoriteButton());

    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(`Remove the favorite from ${LABEL}?`)).toBeTruthy();
    expect(dialog.getByRole("button", { name: "Remove favorite" })).toBeTruthy();
  });

  it("says inline that a marker was not saved, not that the view failed to load", async () => {
    server.use(
      http.put("*/api/v1/workspaces/:workspaceId/pending-discoveries/:pendingId/markers", () =>
        HttpResponse.json(
          { detail: "The posting was removed." },
          { status: 409, headers: { "Content-Type": "application/problem+json" } },
        ),
      ),
    );
    renderMarkers(pendingDiscoveryRow());

    fireEvent.click(favoriteButton());

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Marker not saved: The posting was removed.");
    expect(screen.queryByText(/could not load this view/)).toBeNull();
  });

  it("un-keeps a listed row with no note at once", async () => {
    const item = pendingDiscoveryRow({ favorite: true, kept: true });
    const puts = installMarkers(item);
    renderMarkers(item);

    fireEvent.click(keepButton());

    await waitFor(() => expect(puts.requests).toHaveLength(1));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(puts.requests[0]?.body).toEqual({ favorite: true, kept: false, keep_note: null });
  });
});
