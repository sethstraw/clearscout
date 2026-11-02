import { useMemo } from "react";

import type { Guardrails, GuardrailPhraseMatch, GuardrailPreviewReport } from "../../api/client";
import { useMasterCv, usePreviewGuardrails } from "../../api/hooks";

const RULE_SECTIONS = [
  {
    key: "forbidden" as const,
    label: "Forbidden",
    describe: (matched: boolean) =>
      matched
        ? "Already in your master CV. A draft that copies this text will fail the check."
        : "Not currently in your master CV.",
  },
  {
    key: "required" as const,
    label: "Required",
    describe: (matched: boolean) =>
      matched
        ? "Present as evidence, so a draft can include it."
        : "Missing from your master CV. No redraft can add text that is not evidence; add it to the CV or drop the rule.",
  },
  {
    key: "protected" as const,
    label: "Protected",
    describe: (matched: boolean) =>
      matched
        ? "Mentioned in your master CV. A draft must reproduce it verbatim wherever it appears."
        : "Not currently mentioned in your master CV.",
  },
];

function matchId(rule: string, index: number): string {
  return `guardrail-preview-${rule}-${index}`;
}

/** The master CV's text with every reported match wrapped in a linkable highlight. */
function HighlightedCv({
  content,
  spans,
}: {
  content: string;
  spans: { id: string; start: number; length: number }[];
}) {
  const ordered = useMemo(() => [...spans].sort((a, b) => a.start - b.start), [spans]);
  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  for (const span of ordered) {
    if (span.start < cursor) continue; // an overlapping match; the first claim wins.
    nodes.push(content.slice(cursor, span.start));
    nodes.push(
      <mark id={span.id} key={span.id}>
        {content.slice(span.start, span.start + span.length)}
      </mark>,
    );
    cursor = span.start + span.length;
  }
  nodes.push(content.slice(cursor));
  return <pre className="guardrail-preview-cv">{nodes}</pre>;
}

function PhraseList({
  rule,
  label,
  matches,
  describe,
}: {
  rule: string;
  label: string;
  matches: GuardrailPhraseMatch[];
  describe: (matched: boolean) => string;
}) {
  if (!matches.length) return null;
  return (
    <div>
      <h4>{label}</h4>
      <ul className="record-list">
        {matches.map((match, index) => (
          <li key={`${rule}-${match.phrase}-${index}`}>
            <strong>{match.phrase}</strong>
            <span>{match.matched ? "Present" : "Not found"}</span>
            <small>{describe(match.matched)}</small>
            {match.matched && match.offsets.length > 0 && (
              <small>
                Jump to:{" "}
                {match.offsets.map((offset, offsetIndex) => (
                  <a href={`#${matchId(rule, index)}-${offsetIndex}`} key={offset}>
                    {offsetIndex === 0 ? "first" : `+${offsetIndex}`}
                  </a>
                ))}
              </small>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function precedenceSentence(precedence: GuardrailPreviewReport["work_authorization_precedence"]) {
  if (precedence.drops_cv_line) {
    return "Drafting field will replace this CV line.";
  }
  if (precedence.effective) {
    return "No override; the CV line is used.";
  }
  return "No work-authorization line is configured in either place.";
}

function disagreesInSubstance(
  precedence: GuardrailPreviewReport["work_authorization_precedence"],
): boolean {
  return (
    precedence.drops_cv_line &&
    precedence.cv_line !== null &&
    precedence.drafting_line !== null &&
    precedence.cv_line.trim() !== precedence.drafting_line.trim()
  );
}

export function GuardrailPreviewPanel({
  workspaceId,
  rules,
}: {
  workspaceId: string;
  rules: Guardrails;
}) {
  const masterCv = useMasterCv(workspaceId);
  const preview = usePreviewGuardrails(workspaceId);
  const report = preview.data;

  const spans = useMemo(() => {
    if (!report) return [];
    const built: { id: string; start: number; length: number }[] = [];
    for (const section of RULE_SECTIONS) {
      report[section.key].forEach((match, index) => {
        match.offsets.forEach((offset, offsetIndex) => {
          built.push({
            id: `${matchId(section.key, index)}-${offsetIndex}`,
            start: offset,
            length: match.phrase.length,
          });
        });
      });
    }
    return built;
  }, [report]);

  return (
    <section aria-labelledby="guardrail-preview-heading" className="panel">
      <header>
        <div>
          <h2 id="guardrail-preview-heading">Preview against your master CV</h2>
          <p>
            Runs the same deterministic checks a paid draft would face, against the CV you have now.
            Nothing is saved.
          </p>
        </div>
      </header>
      <div className="panel-body">
        {report && disagreesInSubstance(report.work_authorization_precedence) && (
          <span className="status status--tone-warning" role="status">
            Work-authorization wording disagrees between the CV and the drafting field
          </span>
        )}
        <div className="button-row">
          <button
            className="button button--primary"
            disabled={preview.isPending}
            onClick={() => preview.mutate(rules)}
            type="button"
          >
            {preview.isPending ? "Checking" : "Preview matches"}
          </button>
        </div>
        {preview.isError && <p className="field-error">{preview.error.message}</p>}
        {report && (
          <>
            <p className="field-note">
              Work authorization: {precedenceSentence(report.work_authorization_precedence)}
              {report.work_authorization_precedence.effective &&
                ` The resume will read: "${report.work_authorization_precedence.effective}"`}
            </p>
            {RULE_SECTIONS.map((section) => (
              <PhraseList
                describe={section.describe}
                key={section.key}
                label={section.label}
                matches={report[section.key]}
                rule={section.key}
              />
            ))}
            {spans.length > 0 && masterCv.data && (
              <details>
                <summary>Master CV text with matches highlighted</summary>
                <HighlightedCv content={masterCv.data.content} spans={spans} />
              </details>
            )}
          </>
        )}
      </div>
    </section>
  );
}
