import { useId } from "react";

import type { components } from "../../api/schema";

type Arrangement = components["schemas"]["Arrangement"];
type ArrangementKey = Exclude<
  keyof Arrangement,
  "maximum_office_days" | "maximum_travel_percent" | "relocation" | "onsite_requires_paid_travel"
>;

const ARRANGEMENTS = {
  remote: "Remote",
  hybrid: "Hybrid",
  on_site: "On site",
} satisfies Record<ArrangementKey, string>;
const ARRANGEMENT_ENTRIES = Object.entries(ARRANGEMENTS) as [ArrangementKey, string][];

/**
 * Use one row per arrangement and one column per answer state.
 */
export function AcceptanceMatrix({
  acceptance,
  onChange,
  unsetLabel,
  value,
}: {
  acceptance: string[];
  onChange: (key: ArrangementKey, next: string | null) => void;
  unsetLabel: string;
  value: Partial<Record<ArrangementKey, string | null | undefined>>;
}) {
  const columns = ["", ...acceptance];
  // Keep names unique across rendered matrices.
  const group = useId();
  return (
    <table aria-label="Work arrangement acceptance" className="choice-matrix">
      <thead>
        <tr>
          <td />
          {columns.map((column) => (
            <th key={column || "unset"} scope="col">
              {column || unsetLabel}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {ARRANGEMENT_ENTRIES.map(([key, label]) => (
          <tr key={key}>
            <th scope="row">{label}</th>
            {columns.map((column) => (
              <td key={column || "unset"}>
                <label>
                  <input
                    checked={(value[key] ?? "") === column}
                    name={`${group}-${key}`}
                    onChange={() => onChange(key, column || null)}
                    type="radio"
                  />
                  <span className="visually-hidden">{`${label}: ${column || unsetLabel}`}</span>
                </label>
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
