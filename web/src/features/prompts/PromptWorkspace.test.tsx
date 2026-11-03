import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { PromptDetail, PromptState } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { PromptsPage } from "./PromptWorkspace";

function promptState(
  key: string,
  origin: PromptState["origin"],
  version: number,
  upgrade: PromptState["upgrade"],
): PromptState {
  return {
    key,
    origin,
    version,
    upgrade,
    workspace_id: "workspace-prompts",
    base_sha256: "b".repeat(64),
    content_sha256: "c".repeat(64),
    packaged_sha256: "p".repeat(64),
    updated_at: origin === "override" ? "2026-08-10T00:00:00Z" : null,
    updated_by: origin === "override" ? "human" : "",
    variables: [],
  };
}

const SUITE = [
  promptState("jobspec_system", "packaged", 0, "current"),
  promptState("match_system", "override", 3, "current"),
  promptState("draft_system", "override", 2, "collision"),
  promptState("critic_system", "override", 1, "adopted"),
];

function detailFor(key: string): PromptDetail {
  const state = SUITE.find((item) => item.key === key) ?? SUITE[0]!;
  return {
    ...state,
    key,
    content: `# ${key}\n\n<!--invariant-->\nKeep the evidence boundary.\n<!--/invariant-->\n`,
    packaged_content: `# ${key}\n`,
  };
}

/** Every prompt read the workspace makes, and the keys it opened. */
function installPrompts(): string[] {
  const opened: string[] = [];
  window.localStorage.setItem("clearscout.workspace", "workspace-prompts");
  server.use(
    http.get("*/api/v1/prompts/:key/versions", () => HttpResponse.json([])),
    http.get("*/api/v1/prompts/:key", ({ params }) => {
      const key = String(params.key);
      opened.push(key);
      return HttpResponse.json(detailFor(key));
    }),
    http.get("*/api/v1/prompts", () => HttpResponse.json(SUITE)),
  );
  return opened;
}

describe("PromptsPage", () => {
  it("counts the suite and says an unedited prompt has nothing stored", async () => {
    installPrompts();
    render(
      <AppProviders>
        <PromptsPage />
      </AppProviders>,
    );

    expect(await screen.findByText(/4 prompts, 3 edited/)).toBeTruthy();
  });

  it("labels each prompt by what a later release would do to it", async () => {
    installPrompts();
    render(
      <AppProviders>
        <PromptsPage />
      </AppProviders>,
    );
    await screen.findByText(/4 prompts, 3 edited/);

    expect(screen.getByText("As shipped")).toBeTruthy();
    expect(screen.getByText("Your version 3")).toBeTruthy();
    expect(screen.getByText("Your v2, default moved too")).toBeTruthy();
    expect(screen.getByText("Took the newer default")).toBeTruthy();
  });

  it("says whose text is running when a default moved under an edit", async () => {
    installPrompts();
    render(
      <AppProviders>
        <PromptsPage />
      </AppProviders>,
    );

    expect(await screen.findByText(/Your text is still what runs/)).toBeTruthy();
    expect(screen.getByText(/1 edited prompt has a/)).toBeTruthy();
  });

  it("opens the prompt a reader picks, and marks it as current", async () => {
    const opened = installPrompts();
    render(
      <AppProviders>
        <PromptsPage />
      </AppProviders>,
    );
    await screen.findByText(/4 prompts, 3 edited/);

    const stage = (name: string) =>
      within(screen.getByText(name, { selector: "p" }).closest<HTMLElement>("li")!);
    const first = stage("jobspec").getByRole("button", { name: /System/ });
    expect(first.getAttribute("aria-current")).toBe("true");

    const chosen = stage("draft").getByRole("button", { name: /System/ });
    fireEvent.click(chosen);

    expect(chosen.getAttribute("aria-current")).toBe("true");
    expect(first.getAttribute("aria-current")).toBe("false");
    await waitFor(() => expect(opened).toContain("draft_system"));
  });

  it("shows protected rules and restores an edit inside their fence", async () => {
    installPrompts();
    render(
      <AppProviders>
        <PromptsPage />
      </AppProviders>,
    );

    expect(await screen.findByText("Protected rules (1)")).toBeTruthy();
    expect(screen.getByText("Keep the evidence boundary.", { selector: "pre" })).toBeTruthy();
    const editor = await screen.findByRole("textbox");
    fireEvent.change(editor, {
      target: {
        value: "# jobspec_system\n\n<!--invariant-->\nChanged inside.\n<!--/invariant-->\n",
      },
    });

    expect(editor).toHaveProperty(
      "value",
      "# jobspec_system\n\n<!--invariant-->\nKeep the evidence boundary.\n<!--/invariant-->\n",
    );
  });
});
