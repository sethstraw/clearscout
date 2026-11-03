import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it } from "vitest";

import type { Source, SourceCreate, SourceEdit, SourcePreview } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { sourceList } from "./sourceScanFixtures";
import { SourceManager } from "./SourceManager";

const WORKSPACE = "*/api/v1/workspaces/:workspaceId";

const SOURCES: Source[] = [
  {
    id: "source-1",
    name: "Globex",
    employer: "Globex",
    url: "https://www.globex.com/careers/",
    careers_url: null,
    source_type: "employer_board",
    platform: null,
    priority: 1,
    enabled: true,
    last_scanned_on: "2026-08-16",
    last_status: "failed",
    last_read_outcome: "fetch_failed",
    last_notes: "No board identifier in the page.",
    scan_count: 1,
    staged_count: 0,
    read_scan_count: 0,
    incomplete_scan_count: 0,
    failed_scan_count: 0,
    first_read_on: null,
    last_listed_on: null,
    terms: { url: "", condition: "" },
  },
  {
    id: "source-2",
    name: "Openings Digest",
    employer: null,
    url: "https://digest.test/jobs",
    careers_url: null,
    source_type: "aggregator",
    platform: null,
    priority: 2,
    enabled: false,
    last_scanned_on: null,
    last_status: null,
    last_read_outcome: null,
    last_notes: "",
    scan_count: 0,
    staged_count: 0,
    read_scan_count: 0,
    incomplete_scan_count: 0,
    failed_scan_count: 0,
    first_read_on: null,
    last_listed_on: null,
    terms: {
      url: "https://digest.test/terms",
      condition: "Credit the digest and do not republish its listings.",
    },
  },
];

const NO_PLATFORM = {
  resolved_url: "https://digest.test/jobs",
  platform: null,
  note: "No board platform in this address, so a scan cannot read its openings.",
} satisfies SourcePreview;

/** The text a screen reader announces as the element's description. */
function describedBy(element: HTMLElement): string {
  return (element.getAttribute("aria-describedby") ?? "")
    .split(" ")
    .map((id) => (id ? (document.getElementById(id)?.textContent ?? "") : ""))
    .join(" ");
}

interface Recorded {
  /** How many times the card list was read, so a refetch after a write is visible. */
  listReads: number;
  added: unknown[];
  edits: unknown[];
  retired: string[];
}

function installHandlers({
  preview = NO_PLATFORM,
  revalidatedPlatform = "greenhouse",
  sources = SOURCES,
}: {
  preview?: SourcePreview;
  revalidatedPlatform?: Source["platform"];
  sources?: Source[];
} = {}): Recorded {
  const current = sources.map((source) => ({ ...source }));
  const recorded: Recorded = { listReads: 0, added: [], edits: [], retired: [] };
  server.use(
    http.post(`${WORKSPACE}/sources/preview`, () => HttpResponse.json(preview)),
    http.post(`${WORKSPACE}/sources/:sourceId/revalidate`, ({ params }) => {
      const source = current.find((item) => item.id === params.sourceId);
      if (source) source.platform = revalidatedPlatform;
      return HttpResponse.json(source ?? current[0]);
    }),
    http.post(`${WORKSPACE}/sources`, async ({ request }) => {
      recorded.added.push(await request.json());
      return HttpResponse.json(current[0]);
    }),
    http.patch(`${WORKSPACE}/sources/:sourceId`, async ({ params, request }) => {
      const body = (await request.json()) as SourceEdit;
      recorded.edits.push(body);
      const edited = current.find((item) => item.id === params.sourceId) ?? current[0]!;
      // The careers page belongs to the employer, so every source bound to it shows the change.
      if (body.careers_url != null) {
        for (const item of current.filter((other) => other.employer === edited.employer)) {
          item.careers_url = body.careers_url || null;
        }
      }
      return HttpResponse.json(edited);
    }),
    http.delete(`${WORKSPACE}/sources/:sourceId`, ({ params }) => {
      recorded.retired.push(String(params.sourceId));
      return new HttpResponse(null, { status: 204 });
    }),
    http.get(`${WORKSPACE}/sources`, () => {
      recorded.listReads += 1;
      return HttpResponse.json(sourceList(current));
    }),
  );
  return recorded;
}

describe("SourceManager", () => {
  it("keeps a board no adapter can read and says what the last scan found", async () => {
    installHandlers();

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const card = (await screen.findByText("Globex")).closest("li");
    expect(card).toBeTruthy();
    expect(
      within(card!).getByText(
        "Employer board. Priority 1. No board platform recognized. Check the last read result; an individual posting link may still be readable.",
      ),
    ).toBeTruthy();
    expect(within(card!).getByText(/^Last read: Failed on 8\/16\/2026/)).toBeTruthy();
    expect(within(card!).getByText("No board identifier in the page.")).toBeTruthy();
  });

  it("states a source's condition before it is switched on", async () => {
    const recorded = installHandlers();

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    fireEvent.click(await screen.findByLabelText("Sweep Openings Digest"));

    const consent = screen
      .getByRole("button", { name: "Turn on Openings Digest" })
      .closest<HTMLElement>(".source-consent");
    expect(
      within(consent!).getByText("Credit the digest and do not republish its listings."),
    ).toBeTruthy();
    expect(recorded.edits).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Turn on Openings Digest" }));

    await waitFor(() => expect(recorded.edits).toEqual([{ enabled: true }]));
  });

  it("reveals the compact source details only after the address is validated", async () => {
    installHandlers();

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const form = within(await screen.findByRole("region", { name: "Add a board" }));
    expect(form.getByText("Step 1: Validate the board address")).toBeTruthy();
    expect(form.queryByRole("button", { name: "Add source" })).toBeNull();
    expect(form.queryByLabelText("Name")).toBeNull();

    fireEvent.change(form.getByLabelText("Board or careers address"), {
      target: { value: "https://digest.test/jobs" },
    });
    fireEvent.click(form.getByRole("button", { name: "Validate" }));

    await waitFor(() => expect(form.getByLabelText("Name")).toBeTruthy());
    expect(form.getByRole("group", { name: "Step 2: Name the board" })).toBeTruthy();
    expect(form.getByRole("button", { name: "Add source" }).hasAttribute("disabled")).toBe(true);
    const terms = form.getByText("Advanced: reading condition").closest("details")!;
    expect(terms.open).toBe(false);
    expect(within(terms).getByLabelText("Condition on reading it")).toBeTruthy();
    expect(within(terms).getByLabelText("Published at")).toBeTruthy();
    expect(form.getByLabelText("Name").closest("details")).toBeNull();

    fireEvent.change(form.getByLabelText("Name"), { target: { value: "Openings Digest" } });
    expect(form.getByRole("button", { name: "Add source" }).hasAttribute("disabled")).toBe(false);
  });

  it("invalidates Step 2 when the validated address inputs change", async () => {
    installHandlers();

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const form = within(await screen.findByRole("region", { name: "Add a board" }));
    fireEvent.change(form.getByLabelText("Board or careers address"), {
      target: { value: "https://digest.test/jobs" },
    });
    fireEvent.click(form.getByRole("button", { name: "Validate" }));
    await form.findByRole("group", { name: "Step 2: Name the board" });

    fireEvent.change(form.getByLabelText("Kind"), { target: { value: "aggregator" } });

    expect(form.queryByRole("group", { name: "Step 2: Name the board" })).toBeNull();
    expect(form.queryByRole("button", { name: "Add source" })).toBeNull();
  });

  it("shows the platform Validate resolved before a source is saved", async () => {
    installHandlers({
      preview: {
        resolved_url: "https://job-boards.greenhouse.io/acme",
        platform: "greenhouse",
        note: "Read through the greenhouse API.",
      },
    });

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const form = within(await screen.findByRole("region", { name: "Add a board" }));
    fireEvent.change(form.getByLabelText("Board or careers address"), {
      target: { value: "https://job-boards.greenhouse.io/acme" },
    });
    fireEvent.click(form.getByRole("button", { name: "Validate" }));

    expect(await form.findByText("Read through the greenhouse API.")).toBeTruthy();
  });

  it("registers an aggregator under its own name and records the condition it states", async () => {
    const recorded = installHandlers({ sources: [SOURCES[0]!] });

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const form = within(await screen.findByRole("region", { name: "Add a board" }));
    fireEvent.change(form.getByLabelText("Kind"), { target: { value: "aggregator" } });
    fireEvent.change(form.getByLabelText("Board or careers address"), {
      target: { value: "https://digest.test/jobs" },
    });
    fireEvent.click(form.getByRole("button", { name: "Validate" }));

    fireEvent.change(await form.findByLabelText("Name"), {
      target: { value: "Openings Digest" },
    });
    fireEvent.change(form.getByLabelText("Condition on reading it"), {
      target: { value: "Credit the digest." },
    });
    fireEvent.change(form.getByLabelText("Published at"), {
      target: { value: "https://digest.test/terms" },
    });
    fireEvent.click(form.getByRole("button", { name: "Add source" }));

    await waitFor(() =>
      expect(recorded.added).toEqual([
        {
          name: "Openings Digest",
          url: "https://digest.test/jobs",
          source_type: "aggregator",
          terms: { url: "https://digest.test/terms", condition: "Credit the digest." },
        },
      ]),
    );
  });

  it("revalidates a saved source and updates its platform note", async () => {
    installHandlers({ revalidatedPlatform: "greenhouse" });

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const card = (await screen.findByText("Globex")).closest("li");
    expect(within(card!).getByText(/No board platform recognized/)).toBeTruthy();

    fireEvent.click(within(card!).getByRole("button", { name: "Revalidate" }));

    await waitFor(() =>
      expect(
        within(card!).getByText(
          "Employer board. Priority 1. Recognized platform: greenhouse. Check the last read result for permission and coverage.",
        ),
      ).toBeTruthy(),
    );
  });

  it("sends a corrected address only once it differs from the stored one", async () => {
    const recorded = installHandlers();

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const card = (await screen.findByText("Globex")).closest("li");
    const save = within(card!).getByRole("button", { name: "Save address" });
    expect(save.hasAttribute("disabled")).toBe(true);

    fireEvent.change(within(card!).getByLabelText("Board or careers address"), {
      target: { value: "https://job-boards.greenhouse.io/globex" },
    });
    fireEvent.click(save);

    await waitFor(() =>
      expect(recorded.edits).toEqual([{ url: "https://job-boards.greenhouse.io/globex" }]),
    );
  });

  it("sends a corrected name only once it differs from the stored one", async () => {
    const recorded = installHandlers();

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const card = (await screen.findByText("Globex")).closest("li");
    const save = within(card!).getByRole("button", { name: "Save name" });
    expect(save.hasAttribute("disabled")).toBe(true);

    fireEvent.change(within(card!).getByLabelText("Name"), {
      target: { value: "Globex Corporation" },
    });
    fireEvent.click(save);

    await waitFor(() => expect(recorded.edits).toEqual([{ name: "Globex Corporation" }]));
  });

  it("states in place that another source already holds the name", async () => {
    installHandlers();
    server.use(
      http.patch(`${WORKSPACE}/sources/:sourceId`, () =>
        HttpResponse.json(
          { detail: "Another source is already named Openings Digest." },
          { status: 409 },
        ),
      ),
    );

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const card = (await screen.findByText("Globex")).closest("li");
    fireEvent.change(within(card!).getByLabelText("Name"), {
      target: { value: "Openings Digest" },
    });
    fireEvent.click(within(card!).getByRole("button", { name: "Save name" }));

    expect(
      await within(card!).findByText("Another source is already named Openings Digest."),
    ).toBeTruthy();
  });

  it("saves the company careers page an employer board names", async () => {
    const recorded = installHandlers();

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const card = (await screen.findByText("Globex")).closest("li");
    const save = within(card!).getByRole("button", { name: "Save careers page" });
    expect(save.hasAttribute("disabled")).toBe(true);

    fireEvent.change(within(card!).getByLabelText("Company careers page"), {
      target: { value: " https://www.globex.com/about " },
    });
    fireEvent.click(save);

    await waitFor(() =>
      expect(recorded.edits).toEqual([{ careers_url: "https://www.globex.com/about" }]),
    );
  });

  it("states in place why a job board address is refused as the careers page", async () => {
    installHandlers();
    const refusal =
      "That address is on a job board or applicant tracking system. The company careers page is the employer's own site, which research reads to describe the company.";
    server.use(
      http.patch(`${WORKSPACE}/sources/:sourceId`, () =>
        HttpResponse.json({ detail: refusal }, { status: 422 }),
      ),
    );

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const card = (await screen.findByText("Globex")).closest("li");
    const input = within(card!).getByLabelText("Company careers page");
    fireEvent.change(input, { target: { value: "https://job-boards.greenhouse.io/globex" } });
    fireEvent.click(within(card!).getByRole("button", { name: "Save careers page" }));

    expect((await within(card!).findByRole("alert")).textContent).toBe(refusal);
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(describedBy(input)).toContain(refusal);
    expect(describedBy(input)).toContain("Research reads it; scans read the address above.");
  });

  it.each([
    ["Name", "Save name", "Globex Corporation"],
    ["Board or careers address", "Save address", "https://job-boards.greenhouse.io/globex"],
    ["Condition on reading it", "Save condition", "Credit the board."],
  ])("announces a refused %s edit to assistive technology", async (label, save, value) => {
    installHandlers();
    server.use(
      http.patch(`${WORKSPACE}/sources/:sourceId`, () =>
        HttpResponse.json({ detail: "The edit was refused." }, { status: 409 }),
      ),
    );

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const card = (await screen.findByText("Globex")).closest("li");
    const input = within(card!).getByLabelText(label);
    fireEvent.change(input, { target: { value } });
    fireEvent.click(within(card!).getByRole("button", { name: save }));

    expect((await within(card!).findByRole("alert")).textContent).toBe("The edit was refused.");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(describedBy(input)).toContain("The edit was refused.");
  });

  it("clears a wrong careers page with an empty save", async () => {
    const recorded = installHandlers({
      sources: [{ ...SOURCES[0]!, careers_url: "https://www.globex.com/careers/" }],
    });

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const card = (await screen.findByText("Globex")).closest("li");
    const input = within(card!).getByLabelText<HTMLInputElement>("Company careers page");
    expect(input.value).toBe("https://www.globex.com/careers/");
    fireEvent.change(input, { target: { value: "  " } });
    fireEvent.click(within(card!).getByRole("button", { name: "Save careers page" }));

    await waitFor(() => expect(recorded.edits).toEqual([{ careers_url: "" }]));
    await waitFor(() => expect(input.value).toBe(""));
  });

  it("shows a careers page saved on one card on every card for that employer", async () => {
    installHandlers({
      sources: [
        SOURCES[0]!,
        {
          ...SOURCES[0]!,
          id: "source-3",
          name: "Globex (guided 1)",
          url: "https://job-boards.greenhouse.io/globex",
        },
      ],
    });

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const first = (await screen.findByText("Globex")).closest("li");
    const second = screen.getByText("Globex (guided 1)").closest("li");
    fireEvent.change(within(first!).getByLabelText("Company careers page"), {
      target: { value: "https://www.globex.com/about" },
    });
    fireEvent.click(within(first!).getByRole("button", { name: "Save careers page" }));

    await waitFor(() =>
      expect(within(second!).getByLabelText<HTMLInputElement>("Company careers page").value).toBe(
        "https://www.globex.com/about",
      ),
    );
  });

  it("offers no careers page for an aggregator", async () => {
    installHandlers();

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const card = (await screen.findByText("Openings Digest")).closest("li");
    expect(within(card!).queryByLabelText("Company careers page")).toBeNull();
  });

  it("retires a source only after the consequence is confirmed", async () => {
    const recorded = installHandlers();

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    const card = (await screen.findByText("Globex")).closest("li");
    fireEvent.click(within(card!).getByRole("button", { name: "Retire" }));

    expect(screen.getByText("Retire Globex?")).toBeTruthy();
    expect(recorded.retired).toEqual([]);
    const listReadsBefore = recorded.listReads;

    fireEvent.click(screen.getByRole("button", { name: "Retire source" }));

    await waitFor(() => expect(recorded.retired).toEqual(["source-1"]));
    await waitFor(() => expect(recorded.listReads).toBeGreaterThan(listReadsBefore));
    expect(within(card!).queryByText(/Failed to execute 'json'/)).toBeNull();
  });

  it("turns one board off without touching any other field", async () => {
    const recorded = installHandlers();

    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );

    fireEvent.click(await screen.findByLabelText("Sweep Globex"));

    await waitFor(() => expect(recorded.edits).toEqual([{ enabled: false }]));
  });

  it("retains each batch outcome and retries a failed draft without resaving successes", async () => {
    installHandlers();
    const current = SOURCES.map((source) => ({ ...source }));
    const additions: SourceCreate[] = [];
    let fail = true;
    server.use(
      http.get(`${WORKSPACE}/sources`, () => HttpResponse.json(sourceList(current))),
      http.post(`${WORKSPACE}/sources/preview`, async ({ request }) => {
        const body = (await request.json()) as { url: string };
        return HttpResponse.json({
          resolved_url: body.url,
          platform: body.url.includes("unsupported") ? null : "greenhouse",
          note: "Recognition does not establish reading permission.",
        } satisfies SourcePreview);
      }),
      http.post(`${WORKSPACE}/sources`, async ({ request }) => {
        const body = (await request.json()) as SourceCreate;
        additions.push(body);
        if (body.name === "Initech" && fail)
          return HttpResponse.json({ detail: "Temporary save failure." }, { status: 500 });
        const source = {
          ...SOURCES[0]!,
          ...body,
          id: `added-${additions.length}`,
          platform: body.url.includes("unsupported") ? null : "greenhouse",
        } satisfies Source;
        current.push(source);
        return HttpResponse.json(source);
      }),
    );
    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );
    const form = within(await screen.findByRole("region", { name: "Add a board" }));
    for (let index = 1; index < 4; index++)
      fireEvent.click(form.getByRole("button", { name: "Add another source" }));
    const urls = [
      "https://boards.test/acme",
      "https://unsupported.test/jobs",
      SOURCES[0]!.url,
      "https://boards.test/initech",
    ];
    urls.forEach((url, index) => {
      const row = within(form.getByRole("group", { name: `Source ${index + 1}` }));
      fireEvent.change(row.getByLabelText("Board or careers address"), { target: { value: url } });
    });
    fireEvent.click(form.getByRole("button", { name: "Validate sources" }));
    await waitFor(() => expect(form.getAllByLabelText("Name")).toHaveLength(4));
    ["Acme", "Umbrella", "Globex", "Initech"].forEach((name, index) => {
      const row = within(form.getByRole("group", { name: `Source ${index + 1}` }));
      fireEvent.change(row.getByLabelText("Name"), { target: { value: name } });
    });
    const failed = within(form.getByRole("group", { name: "Source 4" }));
    fireEvent.change(failed.getByLabelText("Condition on reading it"), {
      target: { value: "Credit the board." },
    });
    fireEvent.change(failed.getByLabelText("Published at"), {
      target: { value: "https://boards.test/terms" },
    });
    fireEvent.click(form.getByRole("button", { name: "Add ready sources (4)" }));
    expect(await failed.findByText("Failed")).toBeTruthy();
    expect(within(form.getByRole("group", { name: "Source 1" })).getByText("Added")).toBeTruthy();
    expect(
      within(form.getByRole("group", { name: "Source 2" })).getByText(
        "Saved; board reader not recognized",
      ),
    ).toBeTruthy();
    expect(
      within(form.getByRole("group", { name: "Source 2" })).getByText(
        /Saved for tracking.*individual posting link/,
      ),
    ).toBeTruthy();
    expect(
      within(form.getByRole("group", { name: "Source 3" })).getByText("Duplicate"),
    ).toBeTruthy();
    expect(failed.getByLabelText<HTMLInputElement>("Name").value).toBe("Initech");
    expect(failed.getByLabelText<HTMLTextAreaElement>("Condition on reading it").value).toBe(
      "Credit the board.",
    );
    expect(failed.getByLabelText<HTMLInputElement>("Published at").value).toBe(
      "https://boards.test/terms",
    );
    for (const label of [
      "Board or careers address",
      "Kind",
      "Name",
      "Condition on reading it",
      "Published at",
    ]) {
      const input = failed.getByLabelText(label);
      expect(describedBy(input)).toContain("Temporary save failure.");
      expect(input.getAttribute("aria-invalid")).not.toBe("true");
    }
    expect(additions.map((source) => source.name)).toEqual(["Acme", "Umbrella", "Initech"]);
    const saved = within(screen.getByRole("link", { name: "Globex" }).closest("li")!);
    expect(saved.getByText("No board identifier in the page.")).toBeTruthy();
    expect(saved.getByLabelText<HTMLInputElement>("Sweep Globex").checked).toBe(true);
    fail = false;
    fireEvent.click(failed.getByRole("button", { name: "Add source" }));
    expect(await failed.findByText("Added")).toBeTruthy();
    for (const label of [
      "Board or careers address",
      "Kind",
      "Name",
      "Condition on reading it",
      "Published at",
    ]) {
      expect(describedBy(failed.getByLabelText(label))).not.toContain("Temporary save failure.");
    }
    expect(additions.map((source) => source.name)).toEqual([
      "Acme",
      "Umbrella",
      "Initech",
      "Initech",
    ]);
    expect(form.getByRole("group", { name: "Source 1" })).toBeTruthy();
  });

  it("keeps preview failures editable while validating the other source rows", async () => {
    installHandlers();
    server.use(
      http.post(`${WORKSPACE}/sources/preview`, async ({ request }) => {
        const body = (await request.json()) as { url: string };
        return body.url.includes("failed")
          ? HttpResponse.json({ detail: "The board could not be checked." }, { status: 502 })
          : HttpResponse.json({ ...NO_PLATFORM, resolved_url: body.url });
      }),
    );
    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );
    const form = within(await screen.findByRole("region", { name: "Add a board" }));
    fireEvent.click(form.getByRole("button", { name: "Add another source" }));
    const failed = within(form.getByRole("group", { name: "Source 1" }));
    const second = within(form.getByRole("group", { name: "Source 2" }));
    fireEvent.change(failed.getByLabelText("Board or careers address"), {
      target: { value: "https://failed.test/jobs" },
    });
    fireEvent.change(second.getByLabelText("Board or careers address"), {
      target: { value: "https://boards.test/acme" },
    });
    fireEvent.click(form.getByRole("button", { name: "Validate sources" }));
    expect(await second.findByLabelText("Name")).toBeTruthy();
    expect(failed.getByRole("alert").textContent).toContain("The board could not be checked.");
    expect(describedBy(failed.getByLabelText("Board or careers address"))).toContain(
      "The board could not be checked.",
    );
    expect(describedBy(failed.getByLabelText("Kind"))).toContain("The board could not be checked.");
    expect(failed.getByLabelText<HTMLInputElement>("Board or careers address").value).toBe(
      "https://failed.test/jobs",
    );
    fireEvent.change(failed.getByLabelText("Board or careers address"), {
      target: { value: "https://boards.test/globex" },
    });
    fireEvent.click(failed.getByRole("button", { name: "Validate" }));
    expect(await failed.findByLabelText("Name")).toBeTruthy();
    expect(failed.queryByRole("alert")).toBeNull();
    expect(describedBy(failed.getByLabelText("Board or careers address"))).not.toContain(
      "The board could not be checked.",
    );
  });

  it("states the ready row count before adding and keeps unfinished drafts for correction", async () => {
    const recorded = installHandlers();
    server.use(
      http.post(`${WORKSPACE}/sources/preview`, async ({ request }) => {
        const body = (await request.json()) as { url: string };
        return HttpResponse.json({ ...NO_PLATFORM, resolved_url: body.url });
      }),
    );
    render(
      <AppProviders>
        <SourceManager workspaceId="workspace-sources" />
      </AppProviders>,
    );
    const form = within(await screen.findByRole("region", { name: "Add a board" }));
    for (let index = 1; index < 4; index++)
      fireEvent.click(form.getByRole("button", { name: "Add another source" }));
    const rows = [1, 2, 3, 4].map((number) =>
      within(form.getByRole("group", { name: `Source ${number}` })),
    );
    const urls = [
      "https://boards.test/acme",
      "https://boards.test/umbrella",
      "https://boards.test/initech",
      SOURCES[0]!.url,
    ];
    rows.forEach((row, index) =>
      fireEvent.change(row.getByLabelText("Board or careers address"), {
        target: { value: urls[index] },
      }),
    );
    for (const row of [rows[0]!, rows[1]!, rows[3]!]) {
      fireEvent.click(row.getByRole("button", { name: "Validate" }));
      await row.findByLabelText("Name");
    }
    fireEvent.change(rows[0]!.getByLabelText("Name"), { target: { value: "Acme" } });
    fireEvent.change(rows[3]!.getByLabelText("Name"), { target: { value: "Globex" } });
    fireEvent.click(rows[3]!.getByRole("button", { name: "Add source" }));
    await rows[3]!.findByText("Duplicate");

    expect(
      form.getByText(
        /Only validated rows with a name are ready. Saved and duplicate rows are skipped./,
      ),
    ).toBeTruthy();
    const add = form.getByRole("button", { name: "Add ready sources (1)" });
    expect(add.hasAttribute("disabled")).toBe(false);
    expect(recorded.added).toEqual([]);
    add.focus();
    fireEvent.click(add);

    await rows[0]!.findByText("Saved; board reader not recognized");
    expect(recorded.added).toEqual([
      {
        name: "Acme",
        url: urls[0],
        source_type: "employer_board",
        terms: { condition: "", url: "" },
      },
    ]);
    expect(rows[1]!.getByLabelText<HTMLInputElement>("Name").value).toBe("");
    expect(rows[1]!.getByLabelText<HTMLInputElement>("Board or careers address").value).toBe(
      urls[1],
    );
    expect(rows[2]!.getByLabelText<HTMLInputElement>("Board or careers address").value).toBe(
      urls[2],
    );
    expect(rows[2]!.queryByLabelText("Name")).toBeNull();
    expect(rows[3]!.getByText("Duplicate")).toBeTruthy();
    expect(
      form.getByRole("button", { name: "Add ready sources (0)" }).getAttribute("aria-disabled"),
    ).toBe("true");
    expect(document.activeElement).toBe(add);
    fireEvent.click(add);
    expect(recorded.added).toHaveLength(1);
  });

  it.each([false, true])(
    "retains the %s batch validation launcher and refuses reentry or draft edits",
    async (batch) => {
      installHandlers();
      let release!: () => void;
      const response = new Promise<void>((resolve) => {
        release = resolve;
      });
      let started!: () => void;
      const requestStarted = new Promise<void>((resolve) => {
        started = resolve;
      });
      const previews: string[] = [];
      server.use(
        http.post(`${WORKSPACE}/sources/preview`, async ({ request }) => {
          const body = (await request.json()) as { url: string };
          previews.push(body.url);
          started();
          await response;
          return HttpResponse.json({ ...NO_PLATFORM, resolved_url: body.url });
        }),
      );
      render(
        <AppProviders>
          <SourceManager workspaceId="workspace-sources" />
        </AppProviders>,
      );
      const form = within(await screen.findByRole("region", { name: "Add a board" }));
      if (batch) fireEvent.click(form.getByRole("button", { name: "Add another source" }));
      const row = within(form.getByRole("group", { name: "Source 1" }));
      const address = row.getByLabelText<HTMLInputElement>("Board or careers address");
      fireEvent.change(address, { target: { value: "https://boards.test/acme" } });
      const launcher = form.getByRole("button", { name: batch ? "Validate sources" : "Validate" });
      launcher.focus();
      try {
        await act(async () => {
          fireEvent.click(launcher);
          await requestStarted;
        });
        expect(launcher.matches(":disabled")).toBe(false);
        expect(launcher.getAttribute("aria-disabled")).toBe("true");
        expect(document.activeElement).toBe(launcher);
        expect(address.matches(":disabled")).toBe(true);
        fireEvent.click(launcher);
        fireEvent.change(address, { target: { value: "https://boards.test/changed" } });
        release();
        await row.findByLabelText("Name");
        expect(document.activeElement).toBe(batch ? row.getByLabelText("Name") : launcher);
        expect(address.value).toBe("https://boards.test/acme");
        expect(previews).toEqual(["https://boards.test/acme"]);
        if (batch) fireEvent.click(launcher);
        expect(previews).toHaveLength(1);
      } finally {
        release();
      }
    },
  );

  it.each([false, true])(
    "continues a successful retry without stealing deliberately moved focus: %s",
    async (moveFocus) => {
      installHandlers();
      let release!: () => void;
      const response = new Promise<void>((resolve) => {
        release = resolve;
      });
      let started!: () => void;
      const requestStarted = new Promise<void>((resolve) => {
        started = resolve;
      });
      let attempts = 0;
      server.use(
        http.post(`${WORKSPACE}/sources/preview`, async ({ request }) => {
          const body = (await request.json()) as { url: string };
          return HttpResponse.json({ ...NO_PLATFORM, resolved_url: body.url });
        }),
        http.post(`${WORKSPACE}/sources`, async ({ request }) => {
          const body = (await request.json()) as SourceCreate;
          attempts++;
          if (attempts === 1)
            return HttpResponse.json({ detail: "Temporary save failure." }, { status: 500 });
          started();
          await response;
          return HttpResponse.json({
            ...SOURCES[0]!,
            ...body,
            id: "added-source",
            platform: "greenhouse",
          } satisfies Source);
        }),
      );
      render(
        <AppProviders>
          <SourceManager workspaceId="workspace-sources" />
        </AppProviders>,
      );
      const form = within(await screen.findByRole("region", { name: "Add a board" }));
      const row = within(form.getByRole("group", { name: "Source 1" }));
      const address = row.getByLabelText<HTMLInputElement>("Board or careers address");
      fireEvent.change(address, { target: { value: "https://boards.test/acme" } });
      fireEvent.click(row.getByRole("button", { name: "Validate" }));
      fireEvent.change(await row.findByLabelText("Name"), { target: { value: "Acme" } });
      const launcher = row.getByRole("button", { name: "Add source" });
      launcher.focus();
      fireEvent.click(launcher);
      await row.findByText("Failed");
      expect(document.activeElement).toBe(launcher);
      const elsewhere = screen.getAllByRole("button", { name: "Revalidate" })[0]!;
      try {
        await act(async () => {
          fireEvent.click(launcher);
          await requestStarted;
        });
        expect(launcher.matches(":disabled")).toBe(false);
        expect(document.activeElement).toBe(launcher);
        expect(address.matches(":disabled")).toBe(true);
        fireEvent.click(launcher);
        if (moveFocus) elsewhere.focus();
        release();
        const result = (await row.findByText("Added")).closest("[role=status]");
        expect(attempts).toBe(2);
        expect(document.activeElement).toBe(moveFocus ? elsewhere : result);
      } finally {
        release();
      }
    },
  );
});
