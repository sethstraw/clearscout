import { useId } from "react";

import { AcceptanceMatrix } from "./AcceptanceMatrix";
import {
  numberOrNull,
  wantsOffice as acceptsOffice,
  workingErrors,
  type Draft,
  type SectionProps,
} from "./draft";

function FieldHelp({ error, id, note }: { error: string | null; id: string; note?: string }) {
  if (error) {
    return (
      <p className="field-error" id={id} role="alert">
        {error}
      </p>
    );
  }
  return note ? (
    <small className="field-note" id={id}>
      {note}
    </small>
  ) : null;
}

export function Working({
  acceptance,
  basic = false,
  draft,
  patch,
}: SectionProps & { acceptance: string[]; basic?: boolean }) {
  const formId = useId();
  const set = (changes: Partial<Draft["arrangement"]>) =>
    patch({ arrangement: { ...draft.arrangement, ...changes } });
  const relocation = draft.arrangement.relocation;
  const paidTravel = draft.arrangement.onsite_requires_paid_travel;
  const wantsOffice = acceptsOffice(draft);
  // A remote role with occasional visits still asks the travel question.
  const mayAttend = wantsOffice || draft.arrangement.remote !== null;
  const errors = workingErrors(draft);
  const ids = {
    zone: `${formId}-working-time-zone`,
    shift: `${formId}-working-day-shift-hours`,
    officeDays: `${formId}-maximum-office-days`,
    travel: `${formId}-maximum-travel-percent`,
    paidTravel: `${formId}-paid-office-travel`,
  };
  return (
    <article className="panel">
      <header>
        <div>
          <h2>How you want to work</h2>
          <p>Leaving a row unanswered is not the same as refusing it.</p>
          <p className="field-note">
            Hybrid means a posting states weekly attendance. Somewhere asking for visits less often
            than that reads as remote.
          </p>
        </div>
      </header>
      <div className="panel-body">
        <AcceptanceMatrix
          acceptance={acceptance}
          onChange={(key, next) => set({ [key]: next })}
          unsetLabel="Not stated"
          value={draft.arrangement}
        />
        <div className="settings-grid">
          <div className="compact-field">
            <label htmlFor={ids.zone}>Time zone you normally work from</label>
            <select
              aria-describedby={`${ids.zone}-help`}
              id={ids.zone}
              name={ids.zone}
              onChange={(event) =>
                patch({
                  working_time_zone: (event.target.value || null) as Draft["working_time_zone"],
                  // Shift hours mean nothing without a zone, so clearing the zone clears them.
                  ...(event.target.value ? {} : { working_day_shift_hours: 0 }),
                })
              }
              value={draft.working_time_zone ?? ""}
            >
              <option value="">Not stated</option>
              <option value="Pacific">Pacific</option>
              <option value="Mountain">Mountain</option>
              <option value="Central">Central</option>
              <option value="Eastern">Eastern</option>
            </select>
            <FieldHelp
              error={null}
              id={`${ids.zone}-help`}
              note="Used when a posting requires work within a named North American time zone."
            />
          </div>
          {draft.working_time_zone && (
            <div className="compact-field">
              <label htmlFor={ids.shift}>Hours you can shift your working day</label>
              <input
                aria-describedby={`${ids.shift}-help`}
                aria-invalid={Boolean(errors.shift) || undefined}
                id={ids.shift}
                max={12}
                min={0}
                name={ids.shift}
                onChange={(event) =>
                  patch({ working_day_shift_hours: numberOrNull(event.target.value) ?? 0 })
                }
                placeholder="0"
                step={1}
                type="number"
                value={basic ? draft.working_day_shift_hours : draft.working_day_shift_hours || ""}
              />
              <FieldHelp
                error={errors.shift}
                id={`${ids.shift}-help`}
                note={
                  basic
                    ? "Zero allows no shift for a required time zone. Clear the time zone to leave this restriction unstated. Other numbers allow that many hours earlier or later."
                    : "How many hours earlier or later than usual you can work to match a posting's required time zone."
                }
              />
            </div>
          )}
          {wantsOffice && (
            <div className="compact-field">
              <label htmlFor={ids.officeDays}>Most days in an office per week</label>
              <input
                aria-describedby={errors.officeDays ? `${ids.officeDays}-help` : undefined}
                aria-invalid={Boolean(errors.officeDays) || undefined}
                id={ids.officeDays}
                max={7}
                min={0}
                name={ids.officeDays}
                onChange={(event) => set({ maximum_office_days: numberOrNull(event.target.value) })}
                placeholder="No limit"
                step={1}
                type="number"
                value={draft.arrangement.maximum_office_days ?? ""}
              />
              <FieldHelp error={errors.officeDays} id={`${ids.officeDays}-help`} />
            </div>
          )}
          <div className="compact-field">
            <label htmlFor={ids.travel}>Most travel you will take</label>
            <input
              aria-describedby={`${ids.travel}-help`}
              aria-invalid={Boolean(errors.travel) || undefined}
              id={ids.travel}
              max={100}
              min={0}
              name={ids.travel}
              onChange={(event) =>
                set({ maximum_travel_percent: numberOrNull(event.target.value) })
              }
              placeholder="No limit"
              step={1}
              type="number"
              value={draft.arrangement.maximum_travel_percent ?? ""}
            />
            <FieldHelp
              error={errors.travel}
              id={`${ids.travel}-help`}
              note="Percent of your time."
            />
          </div>
          {mayAttend && (
            <div className="compact-field">
              <label htmlFor={ids.paidTravel}>Office days only if travel is paid</label>
              <select
                aria-describedby={`${ids.paidTravel}-help`}
                id={ids.paidTravel}
                name={ids.paidTravel}
                onChange={(event) =>
                  set({
                    onsite_requires_paid_travel:
                      event.target.value === "" ? null : event.target.value === "yes",
                  })
                }
                value={
                  paidTravel === null || paidTravel === undefined ? "" : paidTravel ? "yes" : "no"
                }
              >
                <option value="">Not stated</option>
                <option value="yes">Yes</option>
                <option value="no">No</option>
              </select>
              <FieldHelp
                error={null}
                id={`${ids.paidTravel}-help`}
                note="The employer covers travel to the office."
              />
            </div>
          )}
          <label className="compact-field" htmlFor={`${formId}-relocation`}>
            Would you relocate
            <select
              id={`${formId}-relocation`}
              name={`${formId}-relocation`}
              onChange={(event) =>
                set({
                  relocation: event.target.value === "" ? null : event.target.value === "yes",
                })
              }
              value={
                relocation === null || relocation === undefined ? "" : relocation ? "yes" : "no"
              }
            >
              <option value="">Not stated</option>
              <option value="yes">Yes</option>
              <option value="no">No</option>
            </select>
          </label>
        </div>
      </div>
    </article>
  );
}
