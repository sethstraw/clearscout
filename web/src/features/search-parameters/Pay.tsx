import type { CompensationBand } from "../../api/client";
import { ActionWithReason } from "../../components/ActionWithReason";
import { EmptyState } from "../../components/States";
import { numberOrNull, placeLabel, type SectionProps } from "./draft";

export function Pay({
  bases,
  basic = false,
  draft,
  patch,
}: SectionProps & { basic?: boolean; bases: CompensationBand["basis"][] }) {
  const keys: string[] = draft.locations.map((item) => item.key).filter(Boolean);
  const update = (index: number, changes: Partial<CompensationBand>) =>
    patch({
      compensation: draft.compensation.map((item, position) =>
        position === index ? { ...item, ...changes } : item,
      ),
    });
  return (
    <article className="panel">
      <header>
        <div>
          <h2>Pay, per place and per currency</h2>
          <p>
            Nothing is converted. A band is compared only against a posting quoting the same
            currency on the same basis, and an unmatched currency abstains.
          </p>
        </div>
        <ActionWithReason
          className="button button--quiet"
          disabledReason={
            keys.length === 0
              ? "Name a place first. A band belongs to somewhere you can work."
              : undefined
          }
          onClick={() =>
            patch({
              compensation: [
                ...draft.compensation,
                {
                  location: keys[0] ?? "",
                  currency: "",
                  basis: "annual",
                  minimum: null,
                  target: null,
                  stretch: null,
                },
              ],
            })
          }
        >
          Add a band
        </ActionWithReason>
      </header>
      <div className="panel-body">
        {keys.length === 0 ? null : draft.compensation.length === 0 ? (
          <EmptyState>No bands yet, so pay is not compared against anything.</EmptyState>
        ) : (
          <ul className="record-list">
            {draft.compensation.map((band, index) => (
              <li key={index}>
                <div className="settings-grid">
                  <label className="compact-field">
                    Place
                    <select
                      onChange={(event) => update(index, { location: event.target.value })}
                      value={band.location}
                    >
                      {keys.map((key) => (
                        <option key={key} value={key}>
                          {basic
                            ? placeLabel(draft.locations.find((place) => place.key === key)!)
                            : key}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="compact-field">
                    Currency
                    <input
                      onChange={(event) => update(index, { currency: event.target.value })}
                      placeholder="As the posting writes it"
                      value={band.currency}
                    />
                  </label>
                  <label className="compact-field">
                    Basis
                    <select
                      onChange={(event) => {
                        const basis = bases.find((value) => value === event.target.value);
                        if (basis) update(index, { basis });
                      }}
                      value={band.basis ?? "annual"}
                    >
                      {bases.map((value) => (
                        <option key={value} value={value}>
                          {value}
                        </option>
                      ))}
                    </select>
                  </label>
                  {(["minimum", "target", "stretch"] as const).map((key) => (
                    <label className="compact-field" key={key}>
                      {key === "minimum" ? "Minimum" : key === "target" ? "Target" : "Stretch"}
                      <input
                        min={0}
                        onChange={(event) =>
                          update(index, { [key]: numberOrNull(event.target.value) })
                        }
                        type="number"
                        value={band[key] ?? ""}
                      />
                    </label>
                  ))}
                </div>
                <button
                  className="button button--danger"
                  onClick={() =>
                    patch({
                      compensation: draft.compensation.filter((_, position) => position !== index),
                    })
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
