import { useId } from "react";

import type { SearchLocation } from "../../api/client";
import { EmptyState } from "../../components/States";
import { AcceptanceMatrix } from "./AcceptanceMatrix";
import { asList, numberOrNull, placeLabel, type Draft, type SectionProps } from "./draft";

export function Places({
  acceptance,
  basic = false,
  draft,
  eligibility,
  patch,
}: SectionProps & { acceptance: string[]; eligibility: string[]; basic?: boolean }) {
  const formId = useId();
  const update = (index: number, changes: Partial<Draft["locations"][number]>) =>
    patch({
      locations: draft.locations.map((item, position) =>
        position === index ? { ...item, ...changes } : item,
      ),
    });
  return (
    <article className="panel">
      <header>
        <div>
          <h2>Places you can work</h2>
          <p>
            A market you have not named reads as undeclared rather than unsuitable. Name none and
            geography gates nothing.
          </p>
          <p>
            Choosing a country or residence does not grant work authorization. Standing is your own
            authorization to work in that market, and the basis is what it rests on, such as
            citizenship or permanent residence. Whether an employer will support you living in
            another country while on that market&apos;s payroll is a separate question that each
            posting answers for itself.
          </p>
        </div>
        <button
          className="button button--quiet"
          onClick={() =>
            patch({
              locations: [
                ...draft.locations,
                {
                  key: basic ? `place-${crypto.randomUUID()}` : "",
                  country: "",
                  region: "",
                  metro: "",
                  eligibility: "",
                  basis: "",
                  aliases: [],
                  arrangement: null,
                  resident: false,
                },
              ],
            })
          }
          type="button"
        >
          Add a place
        </button>
      </header>
      <div className="panel-body">
        {draft.locations.length === 0 ? (
          <EmptyState>
            No places yet. Geography does not rule out any job until you add one.
          </EmptyState>
        ) : (
          <ul className="record-list">
            {draft.locations.map((location, index) => {
              const row = basic ? placeLabel(location) : location.key || `place ${index + 1}`;
              const field = `${formId}-location-${index}`;
              return (
                <li key={index}>
                  <div className="settings-grid">
                    {!basic && (
                      <label className="compact-field" htmlFor={`${field}-key`}>
                        Key
                        <input
                          aria-label={`Key (${row})`}
                          id={`${field}-key`}
                          name={`${field}-key`}
                          onChange={(event) => update(index, { key: event.target.value })}
                          value={location.key}
                        />
                      </label>
                    )}
                    <label className="compact-field" htmlFor={`${field}-country`}>
                      Country
                      <input
                        aria-describedby={`${field}-country-note`}
                        aria-label={`Country (${row})`}
                        id={`${field}-country`}
                        list="country-list"
                        name={`${field}-country`}
                        onChange={(event) => update(index, { country: event.target.value })}
                        value={location.country}
                      />
                      <small className="field-note" id={`${field}-country-note`}>
                        Blank means this row states no country.
                      </small>
                    </label>
                    <label className="compact-field" htmlFor={`${field}-region`}>
                      Region
                      <input
                        aria-describedby={`${field}-region-note`}
                        aria-label={`Region (${row})`}
                        id={`${field}-region`}
                        name={`${field}-region`}
                        onChange={(event) => update(index, { region: event.target.value })}
                        value={location.region ?? ""}
                      />
                      {/* Accept free-form subnational names; country remains a closed list. */}
                      <small className="field-note" id={`${field}-region-note`}>
                        Optional. Province or state, if it narrows the rule.
                      </small>
                    </label>
                    <label className="compact-field" htmlFor={`${field}-metro`}>
                      Metro
                      <input
                        aria-describedby={`${field}-metro-note`}
                        aria-label={`Metro (${row})`}
                        id={`${field}-metro`}
                        name={`${field}-metro`}
                        onChange={(event) => update(index, { metro: event.target.value })}
                        value={location.metro ?? ""}
                      />
                      <small className="field-note" id={`${field}-metro-note`}>
                        Optional. City or metro area, if it narrows the rule within a region.
                      </small>
                    </label>
                    <label className="compact-field" htmlFor={`${field}-eligibility`}>
                      Standing
                      <select
                        aria-label={`Standing (${row})`}
                        id={`${field}-eligibility`}
                        name={`${field}-eligibility`}
                        onChange={(event) =>
                          update(index, {
                            eligibility: event.target.value as SearchLocation["eligibility"],
                          })
                        }
                        value={location.eligibility ?? ""}
                      >
                        <option value="">Choose your standing</option>
                        {eligibility.map((value) => (
                          <option key={value} value={value}>
                            {value}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="compact-field" htmlFor={`${field}-basis`}>
                      Why, in your own words
                      <input
                        aria-label={`Why, in your own words (${row})`}
                        id={`${field}-basis`}
                        name={`${field}-basis`}
                        onChange={(event) => update(index, { basis: event.target.value })}
                        value={location.basis ?? ""}
                      />
                    </label>
                    {!basic && (
                      <label className="compact-field" htmlFor={`${field}-aliases`}>
                        Publisher-specific aliases, comma separated
                        <input
                          aria-describedby={`${field}-aliases-note`}
                          aria-label={`Publisher-specific aliases, comma separated (${row})`}
                          id={`${field}-aliases`}
                          name={`${field}-aliases`}
                          onChange={(event) =>
                            update(index, { aliases: asList(event.target.value) })
                          }
                          placeholder="Greater Toronto Area, GTA"
                          value={(location.aliases ?? []).join(", ")}
                        />
                        <span className="field-note" id={`${field}-aliases-note`}>
                          Common country spellings are built in.
                        </span>
                      </label>
                    )}
                    <div className="compact-field">
                      <div className="checkbox-list">
                        <label htmlFor={`${field}-resident`}>
                          <input
                            aria-describedby={`${field}-resident-note`}
                            aria-label={`I live here (${row})`}
                            checked={location.resident ?? false}
                            id={`${field}-resident`}
                            name={`${field}-resident`}
                            onChange={(event) => update(index, { resident: event.target.checked })}
                            type="checkbox"
                          />
                          I live here
                        </label>
                      </div>
                      <small className="field-note" id={`${field}-resident-note`}>
                        A posting that requires living in another country is refused unless you
                        would relocate (Would you relocate, under How you want to work).
                      </small>
                    </div>
                  </div>
                  {!basic && (
                    <details className="place-arrangement">
                      <summary>
                        Arrangement here
                        <span className="field-note">
                          {location.arrangement
                            ? "overrides the general answers"
                            : "same as everywhere else"}
                        </span>
                      </summary>
                      <AcceptanceMatrix
                        acceptance={acceptance}
                        onChange={(key, value) => {
                          const next = { ...(location.arrangement ?? {}), [key]: value };
                          const empty = Object.values(next).every(
                            (item) => item === null || item === undefined,
                          );
                          update(index, { arrangement: empty ? null : next });
                        }}
                        unsetLabel="Same as everywhere else"
                        value={location.arrangement ?? {}}
                      />
                      <div className="settings-grid">
                        <label className="compact-field" htmlFor={`${field}-maximum-office-days`}>
                          Most days in an office per week
                          <input
                            aria-label={`Most days in an office per week (${row})`}
                            id={`${field}-maximum-office-days`}
                            max={7}
                            min={0}
                            name={`${field}-maximum-office-days`}
                            onChange={(event) => {
                              const next = {
                                ...(location.arrangement ?? {}),
                                maximum_office_days: numberOrNull(event.target.value),
                              };
                              const empty = Object.values(next).every(
                                (value) => value === null || value === undefined,
                              );
                              update(index, { arrangement: empty ? null : next });
                            }}
                            type="number"
                            value={location.arrangement?.maximum_office_days ?? ""}
                          />
                        </label>
                      </div>
                    </details>
                  )}
                  <button
                    aria-label={`Remove ${row}`}
                    className="button button--danger"
                    onClick={() =>
                      patch({
                        locations: draft.locations.filter((_, position) => position !== index),
                        compensation: draft.compensation.filter(
                          (band) => band.location !== location.key,
                        ),
                      })
                    }
                    type="button"
                  >
                    Remove
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </article>
  );
}
