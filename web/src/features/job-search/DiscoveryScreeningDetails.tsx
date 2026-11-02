import type { PendingDiscovery } from "../../api/client";

type StagedFact = NonNullable<NonNullable<PendingDiscovery["staged_facts"]>["locations"]>;
type ScreeningDiscovery = Pick<
  PendingDiscovery,
  "screening_findings" | "screening_standing" | "staged_facts"
>;

function isPublishedFact(value: unknown): value is StagedFact {
  return Boolean(
    value && typeof value === "object" && "values" in value && Array.isArray(value.values),
  );
}

/** Explain deterministic screening without turning unknown evidence into a refusal. */
export function DiscoveryScreeningDetails({ discovery }: { discovery: ScreeningDiscovery }) {
  const findings = discovery.screening_findings;
  const facts: [string, StagedFact][] = [];
  if (discovery.staged_facts) {
    for (const name of [
      "role_aliases",
      "locations",
      "arrangements",
      "employment_types",
      "compensation",
    ] as const) {
      const value = discovery.staged_facts[name];
      if (isPublishedFact(value)) facts.push([name, value]);
    }
  }
  if (!findings.length && !facts.length) return null;
  return (
    <details className="discovery-evidence">
      <summary>Screening evidence and unknowns</summary>
      <p className="field-note">
        {discovery.staged_facts.identity_trust === "publisher"
          ? "Stored employer, title, and location values came from the publisher."
          : "At least one stored identity value remains an unverified hint; published facts below keep their own provenance."}
      </p>
      {findings.length > 0 && (
        <ul>
          {findings.map((finding) => (
            <li key={`${finding.dimension}-${finding.code}`}>
              <strong>{finding.dimension}</strong>: {finding.standing.replaceAll("_", " ")} -{" "}
              {finding.code.replaceAll("_", " ")}
              {finding.subject ? ` (${finding.subject})` : ""}
            </li>
          ))}
        </ul>
      )}
      {facts.length > 0 && (
        <dl className="discovery-facts">
          {facts.map(([name, fact]) => (
            <div key={name}>
              <dt>{name.replaceAll("_", " ")}</dt>
              <dd>
                {(fact.values ?? []).join(", ") || "Unknown"}
                {(fact.unresolved_values ?? []).length > 0
                  ? `; unresolved: ${(fact.unresolved_values ?? []).join(", ")}`
                  : ""}
                <span className="field-note">
                  {fact.provenance || "No provenance retained"};{" "}
                  {fact.complete ? "publisher answer complete" : "publisher answer incomplete"}
                </span>
              </dd>
            </div>
          ))}
        </dl>
      )}
    </details>
  );
}
