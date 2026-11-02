import type { JobDetail } from "../../api/client";
import type { FitReport } from "../../api/client/types";

export type Evidence = JobDetail["evidence"][number];
export type PostingRequirement = NonNullable<JobDetail["requirements"]>[number];

export type Requirement = {
  hardGate: boolean;
  id: string;
  text: string;
  posting: Evidence[];
  record: Evidence[];
  verdicts: { classification: string; strength: string }[];
};

export type FitRequirement = Pick<
  FitReport["requirements"][number],
  "requirement_id" | "is_hard_gate" | "evidence_keys"
>;

// A gate quote cites no requirement; the fit report's stored evidence_keys are
// evidence row ids, so a hard-gate row lists the quotes that belong on its card.
function gateQuotesByRequirement(
  evidence: Evidence[],
  fitRequirements: readonly FitRequirement[],
): Map<string, Evidence[]> {
  const quotes = new Map<string, Evidence[]>();
  for (const gate of fitRequirements) {
    if (!gate.is_hard_gate) continue;
    const keys = new Set(gate.evidence_keys);
    const cited = evidence.filter((item) => !item.requirement_id && keys.has(item.id));
    if (cited.length) quotes.set(gate.requirement_id, cited);
  }
  return quotes;
}

/** Rows that cite no requirement and that no requirement card shows. */
export function unattachedEvidence(
  evidence: Evidence[],
  requirements: readonly PostingRequirement[] = [],
  fitRequirements: readonly FitRequirement[] = [],
): Evidence[] {
  const placed = new Set(
    groupRequirements(evidence, requirements, fitRequirements).flatMap((card) =>
      card.posting.map((item) => item.id),
    ),
  );
  return evidence.filter((item) => !item.requirement_id && !placed.has(item.id));
}

export function groupRequirements(
  evidence: Evidence[],
  requirements: readonly PostingRequirement[] = [],
  fitRequirements: readonly FitRequirement[] = [],
): Requirement[] {
  const textById = new Map<string, string>();
  for (const item of requirements) {
    textById.set(item.requirement_id, item.text);
  }
  const gateQuotes = gateQuotesByRequirement(evidence, fitRequirements);
  const hardGates = new Set(
    fitRequirements.filter((item) => item.is_hard_gate).map((item) => item.requirement_id),
  );
  const byId = new Map<string, Evidence[]>();
  for (const item of evidence) {
    if (!item.requirement_id) continue;
    const group = byId.get(item.requirement_id) ?? [];
    group.push(item);
    byId.set(item.requirement_id, group);
  }
  const idsInOrder = [
    ...requirements.map((item) => item.requirement_id),
    ...[...byId.keys()].filter((id) => !textById.has(id)),
  ];
  const seen = new Set<string>();
  const grouped: Requirement[] = [];
  for (const id of idsInOrder) {
    if (seen.has(id)) continue;
    seen.add(id);
    const items = byId.get(id) ?? [];
    grouped.push({
      hardGate: hardGates.has(id),
      id,
      text: textById.get(id) ?? "",
      posting: [
        ...(gateQuotes.get(id) ?? []),
        ...items.filter((item) => item.source_kind === "posting"),
      ],
      record: items.filter((item) => item.source_kind === "candidate"),
      verdicts: [
        ...new Map(
          items.map(
            (item) =>
              [
                `${item.classification} ${item.strength ?? "none"}`,
                { classification: item.classification, strength: item.strength ?? "none" },
              ] as const,
          ),
        ).values(),
      ],
    });
  }
  return [
    ...grouped.filter((item) => !isAnswered(item)),
    ...grouped.filter((item) => isAnswered(item)),
  ];
}

export function isAnswered(requirement: Requirement): boolean {
  return requirement.record.length > 0;
}
