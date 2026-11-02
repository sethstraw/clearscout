import { useId } from "react";

import { type SectionProps } from "./draft";

const MAX_LENGTH = 4000;

export function OpportunityThesis({
  basic = false,
  draft,
  patch,
}: SectionProps & { basic?: boolean }) {
  const formId = useId();
  const value = draft.opportunity_thesis ?? "";
  return (
    <article className="panel">
      <header>
        <div>
          <h2>What you are looking for</h2>
          <p>Say what the target opportunity looks like, in your own words.</p>
        </div>
      </header>
      <div className="panel-body">
        <label className="compact-field" htmlFor={`${formId}-opportunity-thesis`}>
          {basic ? "Optional work description" : "Opportunity thesis"}
          <textarea
            id={`${formId}-opportunity-thesis`}
            maxLength={MAX_LENGTH}
            name={`${formId}-opportunity-thesis`}
            onChange={(event) => patch({ opportunity_thesis: event.target.value })}
            placeholder="Describe the work and organisations you would prefer."
            rows={6}
            value={value}
          />
          <small className="field-note">
            Ranking reads this as a soft preference. It is not treated as CV evidence and does not
            add search constraints. Leave blank if you have no preference to state.
          </small>
        </label>
        <p className="field-note">
          Preferred level band: {draft.seniority.minimum || "Not stated"} to{" "}
          {draft.seniority.maximum || "Not stated"}. This text does not change them.
        </p>
      </div>
    </article>
  );
}
