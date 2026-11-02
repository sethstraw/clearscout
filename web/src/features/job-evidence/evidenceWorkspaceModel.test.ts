import { describe, expect, it } from "vitest";

import type { Evidence } from "./evidenceWorkspaceModel";
import { groupRequirements, isAnswered, unattachedEvidence } from "./evidenceWorkspaceModel";

function evidence(
  id: string,
  requirementId: string | null,
  sourceKind: "posting" | "candidate",
  classification: Evidence["classification"] = "DIRECT",
  strength: Evidence["strength"] = "strong",
): Evidence {
  return {
    artifact_id: null,
    char_end: null,
    char_start: null,
    classification,
    id,
    locator: `${sourceKind}:${id}`,
    master_cv_entry_id: null,
    observation_id: null,
    requirement_id: requirementId,
    score_links: [],
    source_kind: sourceKind,
    strength,
    text: `Evidence ${id}`,
  };
}

describe("groupRequirements", () => {
  it("groups evidence by requirement and puts unanswered requirements first", () => {
    const grouped = groupRequirements([
      evidence("posting-1", "req-1", "posting"),
      evidence("candidate-1", "req-1", "candidate"),
      evidence("posting-2", "req-2", "posting", "GAP", null),
      evidence("unattached", null, "candidate"),
    ]);

    expect(grouped.map((item) => item.id)).toEqual(["req-2", "req-1"]);
    const [unanswered, answered] = grouped;
    if (!unanswered || !answered) throw new Error("Expected two grouped requirements.");
    expect(unanswered.record).toEqual([]);
    expect(answered.record.map((item) => item.id)).toEqual(["candidate-1"]);
    expect(unanswered.verdicts).toEqual([{ classification: "GAP", strength: "none" }]);
  });

  it("deduplicates repeated classification and strength pairs", () => {
    const grouped = groupRequirements([
      evidence("posting", "req", "posting"),
      evidence("candidate-1", "req", "candidate"),
      evidence("candidate-2", "req", "candidate"),
    ]);

    const [requirement] = grouped;
    if (!requirement) throw new Error("Expected one grouped requirement.");
    expect(requirement.verdicts).toEqual([{ classification: "DIRECT", strength: "strong" }]);
    expect(isAnswered(requirement)).toBe(true);
  });

  it("attaches canonical requirement text and includes requirements with no evidence", () => {
    const grouped = groupRequirements(
      [evidence("candidate-1", "req-1", "candidate")],
      [
        {
          requirement_id: "req-1",
          requirement_key: "key-1",
          text: "Five years of Python.",
          importance: "must_have",
          position: 0,
        },
        {
          requirement_id: "req-2",
          requirement_key: "key-2",
          text: "Familiarity with Agile teams.",
          importance: "nice_to_have",
          position: 1,
        },
      ],
    );

    expect(grouped.map((item) => item.id)).toEqual(["req-2", "req-1"]);
    expect(grouped.map((item) => item.text)).toEqual([
      "Familiarity with Agile teams.",
      "Five years of Python.",
    ]);
    const [uncited] = grouped;
    if (!uncited) throw new Error("Expected an uncited requirement.");
    expect(uncited.posting).toEqual([]);
    expect(uncited.record).toEqual([]);
  });

  const gateCard = {
    requirement_id: "gate-2",
    requirement_key: "key-gate",
    text: "Bachelor's degree required",
    importance: "must_have" as const,
    position: 0,
  };
  const ordinaryCard = {
    requirement_id: "req-1",
    requirement_key: "key-1",
    text: "Five years of synthetic work",
    importance: "must_have" as const,
    position: 1,
  };
  const quote = { ...evidence("ev-quote", null, "posting"), locator: "posting:hard-gate-5" };

  it("places a gate quote on the gate whose fit-report keys cite it, with no verdict", () => {
    const fit = [{ requirement_id: "gate-2", is_hard_gate: true, evidence_keys: ["ev-quote"] }];

    const [gate] = groupRequirements([quote], [gateCard], fit);

    if (!gate) throw new Error("Expected the gate requirement.");
    expect(gate.id).toBe("gate-2");
    expect(gate.posting.map((item) => item.id)).toEqual(["ev-quote"]);
    expect(gate.verdicts).toEqual([]);
    expect(unattachedEvidence([quote], [gateCard], fit)).toEqual([]);
  });

  it("places a folded gate quote on the ordinary requirement that carries it", () => {
    const span = evidence("ev-span", "req-1", "posting");
    const fit = [
      { requirement_id: "req-1", is_hard_gate: true, evidence_keys: ["ev-span", "ev-quote"] },
    ];

    const [carrier] = groupRequirements([span, quote], [ordinaryCard], fit);

    if (!carrier) throw new Error("Expected the carrying requirement.");
    expect(carrier.posting.map((item) => item.id)).toEqual(["ev-quote", "ev-span"]);
    expect(carrier.verdicts).toEqual([{ classification: "DIRECT", strength: "strong" }]);
    expect(unattachedEvidence([span, quote], [ordinaryCard], fit)).toEqual([]);
  });

  it("keeps a quote unattached when no fit report or no hard gate cites it", () => {
    const notGate = [
      { requirement_id: "gate-2", is_hard_gate: false, evidence_keys: ["ev-quote"] },
    ];

    for (const fit of [undefined, [], notGate]) {
      const [gate] = groupRequirements([quote], [gateCard], fit);
      expect(gate?.posting).toEqual([]);
      expect(unattachedEvidence([quote], [gateCard], fit).map((item) => item.id)).toEqual([
        "ev-quote",
      ]);
    }
  });
});
