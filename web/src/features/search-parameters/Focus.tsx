import { useId } from "react";

import type { components } from "../../api/schema";
import { ClusterList } from "./ClusterList";
import { type SectionProps } from "./draft";
import { TokenField } from "./TokenField";

export function Focus({
  caps,
  basic = false,
  draft,
  patch,
}: SectionProps & { basic?: boolean; caps: components["schemas"]["SearchParameterCaps"] }) {
  return (
    <article className="panel">
      <header>
        <div>
          <h2>Focus and exclusions</h2>
          <p>
            A capability is the craft and a domain is the subject matter. Two openings can share a
            title and answer neither, which a title filter alone cannot see.
          </p>
        </div>
      </header>
      <div className="panel-body focus-sections">
        {!basic && (
          <>
            <ClusterList
              addLabel="Add a capability"
              cap={caps.capabilities}
              label="Capabilities"
              noun="Capability"
              onChange={(capabilities) => patch({ focus: { ...draft.focus, capabilities } })}
              termCap={caps.cluster_terms}
              values={draft.focus.capabilities ?? []}
            />
            <ClusterList
              addLabel="Add a domain"
              cap={caps.domains}
              label="Domains"
              noun="Domain"
              onChange={(domains) => patch({ focus: { ...draft.focus, domains } })}
              termCap={caps.cluster_terms}
              values={draft.focus.domains ?? []}
            />
          </>
        )}
        <TokenField
          hint="Work you do not want, as a short phrase such as engineering management. A soft signal, not a hard exclusion like the conditions below: discovery ranks matching postings lower and keeps them, and evaluation marks the posting Deprioritize when its core work matches. Blank rules out nothing."
          label="Role boundaries to steer away from"
          onChange={(values) => patch({ focus: { ...draft.focus, boundaries: values } })}
          placeholder="Type a role boundary, then press Enter"
          values={draft.focus.boundaries ?? []}
        />
        <ExcludedConditions
          onChange={(excluded_conditions) => patch({ excluded_conditions })}
          values={draft.excluded_conditions ?? []}
        />
      </div>
    </article>
  );
}

/** (condition, reason) pairs the semantic review treats as hard conflicts when a posting states one. */
function ExcludedConditions({
  onChange,
  values,
}: {
  onChange: (values: [string, string][]) => void;
  values: [string, string][];
}) {
  const fieldId = useId();
  const setField = (index: number, field: 0 | 1, value: string) =>
    onChange(
      values.map((pair, position): [string, string] =>
        position === index ? (field === 0 ? [value, pair[1]] : [pair[0], value]) : pair,
      ),
    );
  return (
    <section className="cluster-list">
      <div className="cluster-list__header">
        <span>Excluded conditions</span>
        <button
          className="button button--quiet"
          onClick={() => onChange([...values, ["", ""]])}
          type="button"
        >
          Add an excluded condition
        </button>
      </div>
      <p className="field-note">
        The semantic review rules a posting unlikely when it states one of these, citing the reason.
      </p>
      {values.length > 0 && (
        <ol className="record-list">
          {values.map((pair, index) => (
            <li key={index}>
              <div className="settings-grid">
                <label className="compact-field" htmlFor={`${fieldId}-${index}-condition`}>
                  Condition
                  <input
                    id={`${fieldId}-${index}-condition`}
                    name={`${fieldId}-${index}-condition`}
                    onChange={(event) => setField(index, 0, event.target.value)}
                    value={pair[0]}
                  />
                </label>
                <label className="compact-field" htmlFor={`${fieldId}-${index}-reason`}>
                  Reason
                  <input
                    id={`${fieldId}-${index}-reason`}
                    name={`${fieldId}-${index}-reason`}
                    onChange={(event) => setField(index, 1, event.target.value)}
                    value={pair[1]}
                  />
                </label>
              </div>
              <button
                aria-label={`Remove ${pair[0] || "excluded condition"}`}
                className="button button--danger"
                onClick={() => onChange(values.filter((_, position) => position !== index))}
                type="button"
              >
                Remove
              </button>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
