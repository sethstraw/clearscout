import { useId } from "react";

import type { components } from "../../api/schema";
import { EmptyState } from "../../components/States";
import { type SectionProps } from "./draft";
import { TokenField } from "./TokenField";

type CountryNames = components["schemas"]["CountryNames"];

export function CountryTable({ draft, patch }: SectionProps) {
  const fieldId = useId();
  const values = draft.countries;
  const update = (index: number, changes: Partial<CountryNames>) =>
    patch({
      countries: values.map((item, position) =>
        position === index ? { ...item, ...changes } : item,
      ),
    });
  return (
    <article className="panel">
      <header>
        <div>
          <h2>Countries a posting may name</h2>
          <p>
            Name none and the shipped country table applies. Spellings are the alternate forms
            boards write for the same country.
          </p>
        </div>
        <button
          className="button button--quiet"
          onClick={() => patch({ countries: [...values, { country: "", spellings: [] }] })}
          type="button"
        >
          Add a country
        </button>
      </header>
      <div className="panel-body">
        {values.length === 0 ? (
          <EmptyState>No countries yet, so the shipped country table is used as is.</EmptyState>
        ) : (
          <ul className="record-list">
            {values.map((entry, index) => (
              <li key={index}>
                <div className="settings-grid">
                  <label className="compact-field" htmlFor={`${fieldId}-${index}-country`}>
                    Country
                    <input
                      id={`${fieldId}-${index}-country`}
                      list="country-list"
                      name={`${fieldId}-${index}-country`}
                      onChange={(event) => update(index, { country: event.target.value })}
                      value={entry.country}
                    />
                  </label>
                  <TokenField
                    hint="Alternate spellings boards write for this country."
                    label={`Spellings for ${entry.country || "this country"}`}
                    onChange={(spellings) => update(index, { spellings })}
                    placeholder="Type a spelling, then press Enter"
                    values={entry.spellings ?? []}
                  />
                </div>
                <button
                  className="button button--danger"
                  onClick={() =>
                    patch({ countries: values.filter((_, position) => position !== index) })
                  }
                  type="button"
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </article>
  );
}
