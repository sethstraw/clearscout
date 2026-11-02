import { useId, useState } from "react";

import type { ScanScheduleConfiguration, Source } from "../../api/client";
import { useSaveScanSchedule, useScanSchedule } from "../../api/hooks";
import { ApiError } from "../../api/http";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

/** Display an occurrence in the calendar's configured time zone. */
function scheduledTime(instant: string, timezone: string) {
  return `${new Intl.DateTimeFormat("en", {
    timeZone: timezone,
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(instant))} (${timezone})`;
}

/** Configure the workspace's opt-in local scan calendar. */
export function SourceScanSchedule({
  workspaceId,
  sources,
  selectedSourceIds,
}: {
  workspaceId: string;
  sources: Source[];
  selectedSourceIds: readonly string[];
}) {
  const schedule = useScanSchedule(workspaceId);
  const save = useSaveScanSchedule(workspaceId);
  const fieldId = useId();
  const [defaults] = useState<ScanScheduleConfiguration>(() => ({
    frequency: "daily",
    local_time: "09:00",
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    weekday: 0,
    scope: "all_enabled",
    source_ids: [],
    paused: false,
  }));
  const [timezones] = useState(() =>
    typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [],
  );
  const [draft, setDraft] = useState<{
    configuration: ScanScheduleConfiguration;
    revision: number;
  } | null>(null);
  const [errors, setErrors] = useState<
    Partial<Record<"local_time" | "timezone" | "source_ids", string>>
  >({});
  const [notice, setNotice] = useState("");
  const [reloading, setReloading] = useState(false);
  const state = schedule.data?.state;
  const configuration = draft?.configuration ?? state?.configuration ?? defaults;
  const timezoneChoices = Array.from(
    new Set([
      "UTC",
      ...timezones,
      defaults.timezone,
      ...(state ? [state.configuration.timezone] : []),
    ]),
  ).sort();
  const revision = draft?.revision ?? state?.revision ?? 0;
  const conflict =
    (save.error instanceof ApiError && save.error.status === 409) ||
    (draft !== null && revision !== (state?.revision ?? 0));
  const pending = save.isPending || reloading;
  const edit = (patch: Partial<ScanScheduleConfiguration>) => {
    setDraft({ configuration: { ...configuration, ...patch }, revision });
    setErrors({});
    setNotice("");
  };
  const submit = () => {
    const invalid: typeof errors = {};
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(configuration.local_time)) {
      invalid.local_time = "Enter a local time in HH:mm format.";
    }
    try {
      const zone = configuration.timezone.trim();
      if (!zone || /^[+-]/.test(zone)) throw new Error("Unnamed time zone");
      new Intl.DateTimeFormat("en", { timeZone: zone }).format(new Date(0));
    } catch {
      invalid.timezone = "Enter a named IANA time zone, such as America/Winnipeg.";
    }
    if (configuration.scope === "selected" && !configuration.source_ids?.length) {
      invalid.source_ids = "Copy at least one source into the scheduled selection.";
    }
    setErrors(invalid);
    if (Object.keys(invalid).length || pending || conflict) return;
    setNotice("");
    save.mutate(
      {
        ...configuration,
        timezone: configuration.timezone.trim(),
        source_ids: configuration.scope === "selected" ? (configuration.source_ids ?? []) : [],
        expected_revision: revision,
      },
      {
        onSuccess: () => {
          setDraft(null);
          setNotice("Schedule saved.");
        },
      },
    );
  };
  const togglePause = () => {
    if (!state || pending || conflict) return;
    const paused = !state.configuration.paused;
    setNotice("");
    save.mutate(
      {
        ...state.configuration,
        source_ids: state.configuration.source_ids ?? [],
        paused,
        expected_revision: state.revision,
      },
      {
        onSuccess: (result) => {
          if (result.state) {
            const updated = result.state;
            setDraft((current) =>
              current
                ? {
                    configuration: {
                      ...current.configuration,
                      paused: updated.configuration.paused,
                    },
                    revision: updated.revision,
                  }
                : null,
            );
          }
          setNotice(
            paused
              ? "Schedule paused. Active scans continue."
              : "Schedule resumed at its next future occurrence.",
          );
        },
      },
    );
  };
  const reload = async () => {
    setReloading(true);
    try {
      const result = await schedule.refetch();
      if (!result.isError && result.data) {
        setDraft(null);
        setErrors({});
        save.reset();
        setNotice("Saved configuration reloaded; unsaved edits were discarded.");
      }
    } finally {
      setReloading(false);
    }
  };
  if (schedule.isLoading) return <p role="status">Loading scan schedule.</p>;
  if (!schedule.data)
    return (
      <section aria-label="Scan schedule">
        <p className="field-error" role="alert">
          Could not load scan schedule. {schedule.error?.message}
        </p>
        <button
          className="button button--quiet"
          onClick={() => void schedule.refetch()}
          type="button"
        >
          Retry schedule
        </button>
      </section>
    );
  const unavailable = new Set(schedule.data.unavailable_source_ids ?? []);
  const sourceName = (id: string) => sources.find((source) => source.id === id)?.name ?? id;
  return (
    <section className="scan-schedule" aria-labelledby={`${fieldId}-heading`}>
      <h3 id={`${fieldId}-heading`}>Schedule free scans</h3>
      <p>
        Optional, with no provider charge. It only runs while your local TailorCV worker is running.
      </p>
      <p>
        Scheduled board scans require manually saved target titles.{" "}
        <a href="/settings?tab=search">Set up target titles</a>.
      </p>
      {!state ? (
        <p role="status">No scan schedule is enabled.</p>
      ) : (
        <div aria-label="Saved scan schedule">
          <p role="status">
            {state.configuration.paused ? "Schedule paused." : "Schedule enabled."}
          </p>
          <p>
            Saved calendar:{" "}
            {state.configuration.frequency === "daily"
              ? "Daily"
              : WEEKDAYS[state.configuration.weekday]}{" "}
            at {state.configuration.local_time} ({state.configuration.timezone}).
          </p>
          <p>
            Saved source scope:{" "}
            {state.configuration.scope === "all_enabled"
              ? "All enabled sources at each run."
              : (state.configuration.source_ids ?? []).map(sourceName).join(", ") ||
                "No saved sources."}
          </p>
          {state.configuration.paused ? (
            <p>No next occurrence while paused.</p>
          ) : state.next_run_at ? (
            <p>
              Next scheduled scan:{" "}
              <time dateTime={state.next_run_at}>
                {scheduledTime(state.next_run_at, state.configuration.timezone)}
              </time>
            </p>
          ) : (
            <p>No next occurrence recorded.</p>
          )}
          {state.last_due_at && (
            <p>
              Last due occurrence:{" "}
              <time dateTime={state.last_due_at}>
                {scheduledTime(state.last_due_at, state.configuration.timezone)}
              </time>
            </p>
          )}
          {state.last_error && (
            <p role="alert" className="field-error">
              {state.last_error}
            </p>
          )}
          {state.last_task_id && (
            <p>
              <a href="/tasks">View last scheduled scan in Tasks</a>
            </p>
          )}
          <button
            className="button button--quiet"
            disabled={pending || conflict}
            onClick={togglePause}
            type="button"
          >
            {state.configuration.paused ? "Resume schedule" : "Pause schedule"}
          </button>
        </div>
      )}
      {schedule.isError && (
        <p className="field-error" role="alert">
          Could not refresh scan schedule. {schedule.error.message}
        </p>
      )}
      {save.isError && (
        <p className="field-error" role="alert">
          {save.error.message}
        </p>
      )}
      {conflict && (
        <p role="alert">
          The saved schedule changed. Review the saved calendar above, then reload it before saving.
          Your draft is retained.
        </p>
      )}
      <details className="scan-schedule-editor">
        <summary>{state ? "Edit schedule" : "Set up a scan schedule"}</summary>
        <form
          aria-label="Scan schedule configuration"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <fieldset className="compact-field" disabled={pending}>
            <legend>{state ? "Edit scan schedule" : "Enable a scan schedule"}</legend>
            <div className="scan-schedule-fields">
              <div className="compact-field">
                <label htmlFor={`${fieldId}-frequency`}>Frequency</label>
                <select
                  id={`${fieldId}-frequency`}
                  value={configuration.frequency}
                  onChange={(event) =>
                    edit({ frequency: event.target.value === "weekly" ? "weekly" : "daily" })
                  }
                >
                  <option value="daily">Daily</option>
                  <option value="weekly">Weekly</option>
                </select>
              </div>
              {configuration.frequency === "weekly" && (
                <div className="compact-field">
                  <label htmlFor={`${fieldId}-weekday`}>Weekday</label>
                  <select
                    id={`${fieldId}-weekday`}
                    value={configuration.weekday}
                    onChange={(event) => edit({ weekday: Number(event.target.value) })}
                  >
                    {WEEKDAYS.map((weekday, index) => (
                      <option key={weekday} value={index}>
                        {weekday}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <div className="compact-field">
                <label htmlFor={`${fieldId}-time`}>Local time</label>
                <input
                  id={`${fieldId}-time`}
                  type="time"
                  value={configuration.local_time}
                  aria-invalid={Boolean(errors.local_time)}
                  aria-describedby={errors.local_time ? `${fieldId}-time-error` : undefined}
                  onChange={(event) => edit({ local_time: event.target.value })}
                />
                {errors.local_time && (
                  <p id={`${fieldId}-time-error`} role="alert" className="field-error">
                    {errors.local_time}
                  </p>
                )}
              </div>
              <div className="compact-field">
                <label htmlFor={`${fieldId}-timezone`}>Time zone</label>
                <input
                  id={`${fieldId}-timezone`}
                  list={`${fieldId}-timezones`}
                  autoComplete="off"
                  value={configuration.timezone}
                  aria-invalid={Boolean(errors.timezone)}
                  aria-describedby={
                    errors.timezone ? `${fieldId}-timezone-error` : `${fieldId}-timezone-note`
                  }
                  onChange={(event) => edit({ timezone: event.target.value })}
                />
                <datalist id={`${fieldId}-timezones`}>
                  {timezoneChoices.map((timezone) => (
                    <option key={timezone} value={timezone} />
                  ))}
                </datalist>
                <p id={`${fieldId}-timezone-note`} className="field-note">
                  Search by region or city, or enter a named IANA time zone.
                </p>
                {errors.timezone && (
                  <p id={`${fieldId}-timezone-error`} role="alert" className="field-error">
                    {errors.timezone}
                  </p>
                )}
              </div>
            </div>
            <label htmlFor={`${fieldId}-scope`}>Scheduled source scope</label>
            <select
              id={`${fieldId}-scope`}
              value={configuration.scope}
              aria-invalid={Boolean(errors.source_ids)}
              aria-describedby={
                errors.source_ids ? `${fieldId}-scope-error` : `${fieldId}-scope-note`
              }
              onChange={(event) =>
                edit({
                  scope: event.target.value === "selected" ? "selected" : "all_enabled",
                  source_ids: [],
                })
              }
            >
              <option value="all_enabled">All enabled sources at each run</option>
              <option value="selected">Saved source selection</option>
            </select>
            <p id={`${fieldId}-scope-note`} className="field-note">
              All enabled sources changes with your source settings. A saved selection stays fixed
              until you explicitly replace it.
            </p>
            {configuration.scope === "selected" && (
              <>
                <button
                  className="button button--quiet"
                  disabled={selectedSourceIds.length === 0}
                  onClick={() => edit({ source_ids: Array.from(new Set(selectedSourceIds)) })}
                  type="button"
                >
                  Use current source selection
                </button>
                <p className="field-note">
                  Copy {selectedSourceIds.length} source{selectedSourceIds.length === 1 ? "" : "s"}{" "}
                  selected in the manual scan table. Sources excluded from bulk scans can be
                  included.
                </p>
                {(configuration.source_ids ?? []).length > 0 && (
                  <ul aria-label="Scheduled source selection">
                    {(configuration.source_ids ?? []).map((id) => (
                      <li key={id}>
                        {sourceName(id)}
                        {unavailable.has(id) || !sources.some((source) => source.id === id)
                          ? " (unavailable)"
                          : ""}
                      </li>
                    ))}
                  </ul>
                )}
                {unavailable.size > 0 && (
                  <p role="status">
                    Unavailable saved sources remain in the schedule; they are skipped until
                    available or until you replace the saved selection.
                  </p>
                )}
              </>
            )}
            {errors.source_ids && (
              <p id={`${fieldId}-scope-error`} role="alert" className="field-error">
                {errors.source_ids}
              </p>
            )}
            <p id={`${fieldId}-reload-note`} className="field-note">
              Reload saved schedule discards your unsaved edits.
            </p>
            <div className="button-row">
              <button
                className="button button--primary"
                disabled={pending || conflict || (Boolean(state) && !draft)}
                type="submit"
              >
                {save.isPending ? "Saving schedule" : state ? "Save schedule" : "Enable schedule"}
              </button>
              <button
                className="button button--quiet"
                aria-describedby={`${fieldId}-reload-note`}
                disabled={pending || (!draft && !state && !save.isError)}
                onClick={() => void reload()}
                type="button"
              >
                Discard edits and reload saved schedule
              </button>
            </div>
          </fieldset>
        </form>
      </details>
      {draft && <p className="field-note">Unsaved schedule changes.</p>}
      <details className="scan-options">
        <summary>How scheduled scans run</summary>
        <p>
          Enabling or resuming starts at the next future occurrence. After a shutdown, the worker
          queues one catch-up scan for a missed occurrence, then returns to the calendar; it does
          not queue every missed run.
        </p>
        <p>
          Pause stops future scheduled scans. Active scans keep running; cancel them in Tasks.
          Resuming does not catch up occurrences missed while paused.
        </p>
        <p>
          For daylight saving changes, a repeated local time runs at its first occurrence. A time
          skipped when the clocks move forward shifts forward by the size of that gap.
        </p>
      </details>
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
