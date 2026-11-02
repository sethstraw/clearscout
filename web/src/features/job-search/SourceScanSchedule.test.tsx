import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { ScanSchedule, ScanScheduleConfiguration, ScanScheduleInput } from "../../api/client";
import { queryKeys } from "../../api/hooks/keys";
import { server } from "../../test/msw/server";
import { SourceScanSchedule } from "./SourceScanSchedule";
import { boardSource } from "./sourceScanFixtures";

const SOURCES = [boardSource(), boardSource({ id: "source-2", name: "Initech", enabled: false })];
const ROUTE = "*/api/v1/workspaces/workspace-1/scan-schedule";
const CONFIGURATION: ScanScheduleConfiguration = {
  frequency: "daily",
  local_time: "09:00",
  timezone: "America/Winnipeg",
  weekday: 0,
  scope: "all_enabled",
  source_ids: [],
  paused: false,
};

function savedSchedule(overrides: Partial<NonNullable<ScanSchedule["state"]>> = {}): ScanSchedule {
  return {
    workspace_id: "workspace-1",
    unavailable_source_ids: [],
    state: {
      configuration: CONFIGURATION,
      revision: 1,
      next_run_at: "2026-10-09T14:00:00Z",
      last_due_at: null,
      last_task_id: null,
      last_error: null,
      ...overrides,
    },
  };
}

function scheduleServer(initial: ScanSchedule = { workspace_id: "workspace-1", state: null }) {
  let current = initial;
  const submitted: ScanScheduleInput[] = [];
  server.use(
    http.get(ROUTE, () => HttpResponse.json(current)),
    http.put(ROUTE, async ({ request }) => {
      const input = (await request.json()) as ScanScheduleInput;
      submitted.push(input);
      const { expected_revision, ...configuration } = input;
      current = savedSchedule({
        configuration,
        revision: expected_revision + 1,
        next_run_at: configuration.paused ? null : "2026-10-09T14:00:00Z",
      });
      return HttpResponse.json(current);
    }),
  );
  return {
    submitted,
    update: (schedule: ScanSchedule) => {
      current = schedule;
    },
  };
}

function schedulePanel(selectedSourceIds: readonly string[] = [], initial?: ScanSchedule) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } },
  });
  if (initial) client.setQueryData(queryKeys.scanSchedule("workspace-1"), initial);
  const view = (selected: readonly string[]) => (
    <QueryClientProvider client={client}>
      <SourceScanSchedule
        workspaceId="workspace-1"
        sources={SOURCES}
        selectedSourceIds={selected}
      />
    </QueryClientProvider>
  );
  const rendered = render(view(selectedSourceIds));
  return { client, rerender: (selected: readonly string[]) => rendered.rerender(view(selected)) };
}

async function openScheduleEditor() {
  fireEvent.click(await screen.findByText(/^(Set up a scan schedule|Edit schedule)$/));
}

describe("SourceScanSchedule", () => {
  it("keeps optional setup compact and retains edits through disclosure toggles", async () => {
    const { submitted } = scheduleServer();
    schedulePanel();

    const summary = await screen.findByText("Set up a scan schedule");
    const disclosure = summary.closest("details")!;
    expect(disclosure.open).toBe(false);
    expect(screen.getByText("No scan schedule is enabled.")).toBeTruthy();
    expect(screen.getByText(/It runs only while TailorCV is running/)).toBeTruthy();
    fireEvent.click(summary);
    expect(disclosure.open).toBe(true);
    fireEvent.change(screen.getByLabelText("Local time"), { target: { value: "11:30" } });
    fireEvent.click(summary);
    expect(disclosure.open).toBe(false);
    expect(screen.getByText("Unsaved schedule changes.").closest("details")).toBeNull();
    fireEvent.click(summary);
    expect(screen.getByLabelText<HTMLInputElement>("Local time").value).toBe("11:30");
    expect(submitted).toEqual([]);
  });

  it("offers browser time zones, UTC, and a local alias for a new calendar", async () => {
    const local = vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      ...new Intl.DateTimeFormat().resolvedOptions(),
      timeZone: "Canada/Eastern",
    });
    const supported = vi.spyOn(Intl, "supportedValuesOf").mockReturnValue(["Europe/Paris"]);
    try {
      scheduleServer();
      schedulePanel();
      await openScheduleEditor();

      const zone = screen.getByRole<HTMLInputElement>("combobox", { name: "Time zone" });
      expect(zone.value).toBe("Canada/Eastern");
      expect(screen.getByLabelText<HTMLInputElement>("Local time").value).toBe("09:00");
      const choices = document.getElementById(zone.getAttribute("list")!)!;
      expect(
        within(choices)
          .getAllByRole<HTMLOptionElement>("option", { hidden: true })
          .map((option) => option.value),
      ).toEqual(["Canada/Eastern", "Europe/Paris", "UTC"]);
      zone.focus();
      expect(document.activeElement).toBe(zone);
    } finally {
      local.mockRestore();
      supported.mockRestore();
    }
  });

  it("retains the saved zone and useful editable choices without browser zone enumeration", async () => {
    const local = vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      ...new Intl.DateTimeFormat().resolvedOptions(),
      timeZone: "America/Toronto",
    });
    vi.stubGlobal(
      "Intl",
      new Proxy(Intl, {
        get(target, property) {
          return property === "supportedValuesOf"
            ? undefined
            : (Reflect.get(target, property) as unknown);
        },
      }),
    );
    try {
      const { submitted } = scheduleServer(
        savedSchedule({
          configuration: { ...CONFIGURATION, timezone: "Pacific/Chatham" },
          last_error: "Target titles must be saved before scheduling.",
        }),
      );
      schedulePanel();

      const summary = await screen.findByText("Edit schedule");
      expect(summary.closest("details")!.open).toBe(false);
      expect(
        screen.getByText(/Saved calendar: Daily at 09:00 \(Pacific\/Chatham\)/).closest("details"),
      ).toBeNull();
      expect(screen.getByText(/Next scheduled scan:/).closest("details")).toBeNull();
      expect(
        screen.getByText("Target titles must be saved before scheduling.").closest("details"),
      ).toBeNull();
      expect(screen.getByRole("button", { name: "Pause schedule" })).toBeTruthy();
      fireEvent.click(summary);
      const zone = screen.getByRole<HTMLInputElement>("combobox", { name: "Time zone" });
      expect(zone.value).toBe("Pacific/Chatham");
      const choices = document.getElementById(zone.getAttribute("list")!)!;
      expect(
        within(choices)
          .getAllByRole<HTMLOptionElement>("option", { hidden: true })
          .map((option) => option.value),
      ).toEqual(["America/Toronto", "Pacific/Chatham", "UTC"]);
      fireEvent.change(zone, { target: { value: "Europe/London" } });
      fireEvent.submit(screen.getByRole("form", { name: "Scan schedule configuration" }));
      await screen.findByText("Schedule saved.");
      expect(submitted).toHaveLength(1);
      expect(submitted[0]).toMatchObject({ timezone: "Europe/London", expected_revision: 1 });
    } finally {
      vi.unstubAllGlobals();
      local.mockRestore();
    }
  });

  it("leaves scheduling disabled until explicit opt-in and explains the local limits", async () => {
    const { submitted } = scheduleServer();
    schedulePanel();
    await openScheduleEditor();

    await screen.findByRole("button", { name: "Enable schedule" });
    expect(screen.getByText("No scan schedule is enabled.")).toBeTruthy();
    expect(screen.getByRole<HTMLInputElement>("combobox", { name: "Time zone" }).value).toBe(
      Intl.DateTimeFormat().resolvedOptions().timeZone,
    );
    expect(screen.getByLabelText<HTMLInputElement>("Local time").value).toBe("09:00");
    expect(screen.getByText(/It runs only while TailorCV is running/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "How scheduled scans run" }));
    const how = screen.getByRole("dialog", { name: "How scheduled scans run" });
    expect(within(how).getByText(/one catch-up scan, not every missed run/)).toBeTruthy();
    expect(within(how).getByText(/Resuming skips runs missed while paused/)).toBeTruthy();
    expect(within(how).getByText(/a repeated time runs once/)).toBeTruthy();
    expect(within(how).getByText(/shifts forward by the gap/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: /target titles/ })).toBeNull();
    expect(submitted).toEqual([]);
  });

  it.each(["daily", "weekly"] as const)(
    "saves an opted-in %s calendar through the accessible form",
    async (frequency) => {
      const { submitted } = scheduleServer();
      schedulePanel();
      await openScheduleEditor();

      const zone = await screen.findByRole<HTMLInputElement>("combobox", { name: "Time zone" });
      fireEvent.change(zone, { target: { value: "America/Winnipeg" } });
      fireEvent.change(screen.getByRole("combobox", { name: "Frequency" }), {
        target: { value: frequency },
      });
      if (frequency === "weekly")
        fireEvent.change(screen.getByRole("combobox", { name: "Weekday" }), {
          target: { value: "4" },
        });
      zone.focus();
      expect(document.activeElement).toBe(zone);
      fireEvent.submit(screen.getByRole("form", { name: "Scan schedule configuration" }));

      await screen.findByText("Schedule saved.");
      expect(submitted).toEqual([
        {
          ...CONFIGURATION,
          frequency,
          weekday: frequency === "weekly" ? 4 : 0,
          expected_revision: 0,
        },
      ]);
      expect(screen.getByText("Schedule enabled.")).toBeTruthy();
      const next = screen.getByText(/Next scheduled scan:/);
      expect(next.textContent).toContain("09:00");
      expect(next.textContent).toContain("America/Winnipeg");
      expect(within(next).getByText(/09:00/).getAttribute("datetime")).toBe("2026-10-09T14:00:00Z");
      if (frequency === "weekly") expect(screen.getByText(/Friday at 09:00/)).toBeTruthy();
    },
  );

  it("copies a source subset explicitly and keeps it when the manual selection changes", async () => {
    const { submitted } = scheduleServer();
    const { rerender } = schedulePanel(["source-2"]);
    await openScheduleEditor();

    await screen.findByRole("button", { name: "Enable schedule" });
    fireEvent.change(screen.getByRole("combobox", { name: "Time zone" }), {
      target: { value: "America/Winnipeg" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Scheduled source scope" }), {
      target: { value: "selected" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Use current source selection" }));
    rerender(["source-1"]);

    const scope = screen.getByRole("list", { name: "Scheduled source selection" });
    expect(within(scope).getByText("Initech")).toBeTruthy();
    expect(within(scope).queryByText("Northwind")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Enable schedule" }));
    await screen.findByText("Schedule saved.");
    expect(submitted[0]).toEqual({
      ...CONFIGURATION,
      scope: "selected",
      source_ids: ["source-2"],
      expected_revision: 0,
    });
  });

  it("associates local-time, zone, and empty-subset errors with their controls", async () => {
    const { submitted } = scheduleServer();
    schedulePanel();
    await openScheduleEditor();

    await screen.findByRole("button", { name: "Enable schedule" });
    const time = screen.getByLabelText<HTMLInputElement>("Local time");
    const zone = screen.getByRole<HTMLInputElement>("combobox", { name: "Time zone" });
    const scope = screen.getByRole<HTMLSelectElement>("combobox", {
      name: "Scheduled source scope",
    });
    fireEvent.change(time, { target: { value: "" } });
    fireEvent.change(zone, { target: { value: "Not/AZone" } });
    fireEvent.change(scope, { target: { value: "selected" } });
    fireEvent.click(screen.getByRole("button", { name: "Enable schedule" }));

    for (const [control, message] of [
      [time, "Enter a local time in HH:mm format."],
      [zone, "Enter a named IANA time zone, such as America/Winnipeg."],
      [scope, "Copy at least one source into the scheduled selection."],
    ] as const) {
      expect(control.getAttribute("aria-invalid")).toBe("true");
      const error = screen.getByText(message);
      expect(control.getAttribute("aria-describedby")?.split(" ")).toContain(error.id);
    }
    expect(submitted).toEqual([]);
  });

  it("pauses the saved calendar without saving a draft and resumes at a future occurrence", async () => {
    const { submitted } = scheduleServer(savedSchedule());
    schedulePanel();
    await openScheduleEditor();

    await screen.findByRole("button", { name: "Pause schedule" });
    fireEvent.change(screen.getByLabelText("Local time"), { target: { value: "11:30" } });
    fireEvent.click(screen.getByRole("button", { name: "Pause schedule" }));
    await screen.findByRole("button", { name: "Resume schedule" });
    expect(screen.getByText("No next occurrence while paused.")).toBeTruthy();
    expect(screen.getByLabelText<HTMLInputElement>("Local time").value).toBe("11:30");
    expect(submitted[0]).toEqual({ ...CONFIGURATION, paused: true, expected_revision: 1 });
    expect(screen.getByText("Schedule paused. Active scans continue.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Resume schedule" }));
    await screen.findByText("Schedule resumed at its next future occurrence.");
    expect(submitted[1]).toEqual({ ...CONFIGURATION, expected_revision: 2 });
    expect(screen.getByLabelText<HTMLInputElement>("Local time").value).toBe("11:30");
    fireEvent.click(screen.getByRole("button", { name: "Save schedule" }));
    await screen.findByText("Schedule saved.");
    expect(submitted[2]).toEqual({ ...CONFIGURATION, local_time: "11:30", expected_revision: 3 });
  });

  it("keeps unavailable saved IDs visible and eligible for pause", async () => {
    const initial = savedSchedule({
      configuration: {
        ...CONFIGURATION,
        scope: "selected",
        source_ids: ["source-2", "retired-source"],
      },
      last_error: "Manually saved target titles are required.",
      last_task_id: "task-1",
    });
    initial.unavailable_source_ids = ["retired-source"];
    const { submitted } = scheduleServer(initial);
    schedulePanel();
    await openScheduleEditor();

    await screen.findByRole("button", { name: "Pause schedule" });
    const scope = screen.getByRole("list", { name: "Scheduled source selection" });
    expect(within(scope).getByText("Initech")).toBeTruthy();
    expect(within(scope).getByText("retired-source (unavailable)")).toBeTruthy();
    expect(screen.getByText("Manually saved target titles are required.")).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "View last scheduled scan in Tasks" }).getAttribute("href"),
    ).toBe("/tasks");
    fireEvent.click(screen.getByRole("button", { name: "Pause schedule" }));
    await screen.findByRole("button", { name: "Resume schedule" });
    expect(submitted[0]).toMatchObject({
      source_ids: ["source-2", "retired-source"],
      paused: true,
    });
  });

  it("retains the draft when saving fails", async () => {
    scheduleServer(savedSchedule());
    server.use(
      http.put(ROUTE, () =>
        HttpResponse.json(
          { detail: "Target titles must be saved before scheduling." },
          { status: 422 },
        ),
      ),
    );
    schedulePanel();
    await openScheduleEditor();

    await screen.findByRole("button", { name: "Pause schedule" });
    fireEvent.change(screen.getByLabelText("Local time"), { target: { value: "11:30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save schedule" }));
    await screen.findByText("Target titles must be saved before scheduling.");
    expect(screen.getByLabelText<HTMLInputElement>("Local time").value).toBe("11:30");
    expect(
      screen
        .getByRole("link", { name: "Check target titles in Settings > Search" })
        .getAttribute("href"),
    ).toBe("/settings?tab=search");
  });

  it("retains a conflicting draft until the saved configuration is explicitly reloaded", async () => {
    const transport = scheduleServer(savedSchedule());
    const submitted: ScanScheduleInput[] = [];
    server.use(
      http.put(ROUTE, async ({ request }) => {
        submitted.push((await request.json()) as ScanScheduleInput);
        transport.update(
          savedSchedule({ configuration: { ...CONFIGURATION, local_time: "10:00" }, revision: 2 }),
        );
        return HttpResponse.json(
          { detail: "Schedule changed in another window." },
          { status: 409 },
        );
      }),
    );
    schedulePanel();
    await openScheduleEditor();

    await screen.findByRole("button", { name: "Pause schedule" });
    fireEvent.change(screen.getByLabelText("Local time"), { target: { value: "11:30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save schedule" }));
    await screen.findByText("Schedule changed in another window.");
    expect(screen.getByLabelText<HTMLInputElement>("Local time").value).toBe("11:30");
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Save schedule" }).disabled).toBe(
      true,
    );
    await screen.findByText(/Saved calendar: Daily at 10:00/);
    expect(submitted).toHaveLength(1);
    expect(submitted[0]).toMatchObject({ expected_revision: 1 });
    expect(screen.getByText("Reload saved schedule discards your unsaved edits.")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Discard edits and reload saved schedule" }),
    );
    await screen.findByText("Saved configuration reloaded; unsaved edits were discarded.");
    expect(screen.getByLabelText<HTMLInputElement>("Local time").value).toBe("10:00");
    expect(screen.queryByText("Schedule changed in another window.")).toBeNull();
  });

  it("keeps a dirty draft during a polling readback while refreshing runtime state", async () => {
    vi.useFakeTimers();
    let unsubscribe = () => {};
    let client: QueryClient | undefined;
    try {
      scheduleServer(
        savedSchedule({ next_run_at: "2026-10-10T14:00:00Z", last_due_at: "2026-10-09T14:00:00Z" }),
      );
      ({ client } = schedulePanel([], savedSchedule()));
      fireEvent.click(screen.getByText("Edit schedule"));
      fireEvent.change(screen.getByLabelText("Local time"), { target: { value: "11:30" } });
      const readback = new Promise<void>((resolve) => {
        unsubscribe = client!.getQueryCache().subscribe((event) => {
          const data = event.query.state.data as ScanSchedule | undefined;
          if (data?.state?.last_due_at === "2026-10-09T14:00:00Z") resolve();
        });
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
        await readback;
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(screen.getByLabelText<HTMLInputElement>("Local time").value).toBe("11:30");
      expect(screen.getByText(/Next scheduled scan:/).textContent).toContain("Oct 10");
      expect(screen.getByText(/Last due occurrence:/).textContent).toContain("Oct 9");
    } finally {
      unsubscribe();
      client?.clear();
      vi.useRealTimers();
    }
  });
});
