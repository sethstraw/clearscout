import { useId, useRef } from "react";

import type { components } from "../../api/schema";
import { type MoveDirection, OrderControls, UsedFirstNote } from "./ListCap";
import { TokenField } from "./TokenField";

type Cluster = components["schemas"]["Cluster"];

/** Ordered clusters of terms; the terms rank a posting, the name only labels the group. */
export function ClusterList({
  addLabel,
  cap,
  label,
  noun,
  onChange,
  termCap,
  values,
}: {
  addLabel: string;
  cap: number;
  label: string;
  noun: string;
  onChange: (values: Cluster[]) => void;
  termCap: number;
  values: Cluster[];
}) {
  const fieldId = useId();
  const moveButtons = useRef(new Map<string, HTMLButtonElement | null>());
  const nameInputs = useRef(new Map<number, HTMLInputElement | null>());
  const pluralNoun = label.toLowerCase();
  // Mirrors Focus.ranked_clusters: blank names are skipped, then the first `cap` named rows rank.
  const ranked = values.reduce<(boolean | null)[]>((flags, cluster) => {
    if (!cluster.name.trim()) return [...flags, null];
    const position = flags.filter((flag) => flag !== null).length;
    return [...flags, position < cap];
  }, []);
  const named = ranked.filter((flag) => flag !== null).length;
  const update = (index: number, changes: Partial<Cluster>) =>
    onChange(values.map((item, position) => (position === index ? { ...item, ...changes } : item)));
  const move = (from: number, to: number, direction: MoveDirection) => {
    const next = [...values];
    [next[from], next[to]] = [next[to]!, next[from]!];
    onChange(next);
    requestAnimationFrame(() => {
      const button = moveButtons.current.get(`${to}-${direction}`);
      if (button && !button.disabled) button.focus();
      else nameInputs.current.get(to)?.focus();
    });
  };
  return (
    <section className="cluster-list">
      <div className="cluster-list__header">
        <span>{label}</span>
        <button
          className="button button--quiet"
          onClick={() => onChange([...values, { name: "", terms: [], because: "" }])}
          type="button"
        >
          {addLabel}
        </button>
      </div>
      <UsedFirstNote cap={cap} count={named} noun={pluralNoun} />
      {values.length > 0 && (
        <ol className="record-list">
          {values.map((cluster, index) => (
            <li className={ranked[index] === false ? "list-cap-unused" : undefined} key={index}>
              <div className="settings-grid">
                <label className="compact-field" htmlFor={`${fieldId}-${index}-name`}>
                  {noun} name
                  <input
                    aria-describedby={
                      ranked[index] === false ? `${fieldId}-${index}-ranked` : undefined
                    }
                    id={`${fieldId}-${index}-name`}
                    name={`${fieldId}-${index}-name`}
                    onChange={(event) => update(index, { name: event.target.value })}
                    ref={(element) => {
                      nameInputs.current.set(index, element);
                    }}
                    value={cluster.name}
                  />
                </label>
                <TokenField
                  hint="A title is read against these terms, not the name."
                  label={`${noun} terms`}
                  max={termCap}
                  onChange={(terms) => update(index, { terms })}
                  placeholder="Type a term, then press Enter"
                  values={cluster.terms ?? []}
                />
                <label className="compact-field" htmlFor={`${fieldId}-${index}-because`}>
                  Why this {noun.toLowerCase()} matters
                  <input
                    id={`${fieldId}-${index}-because`}
                    name={`${fieldId}-${index}-because`}
                    onChange={(event) => update(index, { because: event.target.value })}
                    value={cluster.because}
                  />
                </label>
              </div>
              <div className="button-row">
                {ranked[index] === false && (
                  <span
                    className="search-suggestion-status search-suggestion-status--rejected list-cap-badge"
                    id={`${fieldId}-${index}-ranked`}
                  >
                    Not ranked
                  </span>
                )}
                <OrderControls
                  count={values.length}
                  index={index}
                  label={cluster.name || `${noun.toLowerCase()} ${index + 1}`}
                  onMove={move}
                  onRef={(direction, element) => {
                    moveButtons.current.set(`${index}-${direction}`, element);
                  }}
                />
                <button
                  className="button button--danger"
                  onClick={() => onChange(values.filter((_, position) => position !== index))}
                  type="button"
                >
                  Remove
                </button>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
