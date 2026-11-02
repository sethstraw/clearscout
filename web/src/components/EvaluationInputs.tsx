import type { Job } from "../api/client";

const labels = {
  cv: "CV",
  search_parameters: "Search parameters",
  posting: "Posting",
  prompts: "Prompts",
  build: "Application build",
};

export function EvaluationInputs({
  evaluated,
  inputs,
  detail = false,
}: {
  evaluated: boolean;
  inputs: Job["evaluation_inputs"];
  detail?: boolean;
}) {
  if (!evaluated) return <span className="field-note">Not evaluated</span>;
  const changed = inputs?.filter((input) => input.status === "changed") ?? [];
  const unknown = inputs?.filter((input) => input.status === "unknown") ?? [];
  const summary = !inputs?.length
    ? "Historical score: input identities unknown"
    : changed.length
      ? `Historical score: ${changed.map((input) => labels[input.name]).join(", ")} changed`
      : unknown.length
        ? "Historical score: some input identities unknown"
        : "Assessed with current inputs";
  if (!detail) return <span className="field-note">{summary}</span>;
  return (
    <div>
      <p>{summary}. A saved score is never recalculated by viewing it.</p>
      <dl>
        {(inputs ?? []).map((input) => (
          <div key={input.name}>
            <dt>
              {labels[input.name]}: {input.status}
            </dt>
            <dd>
              Recorded: {input.recorded ?? "unknown"}. Current: {input.current ?? "unknown"}.
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
