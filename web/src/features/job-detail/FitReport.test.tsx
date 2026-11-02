import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { FitReport as FitReportData } from "../../api/client/types";
import { FitReport } from "./FitReport";

const report: FitReportData = {
  job_posting_id: "job-1",
  evaluation_id: "evaluation-1",
  evaluation_version: 2,
  artifact_id: "artifact-1",
  sha256: "a".repeat(64),
  markdown: "# Stored report",
  requirements: [
    {
      requirement_id: "requirement-1",
      requirement_key: "req-key-1",
      importance: "must_have",
      text: "Lead a data platform",
      evidence_keys: [],
      span_status: "verified",
      established: true,
      unmet: false,
      dimension: "hard",
      is_hard_gate: false,
    },
    {
      requirement_id: "gate-1",
      requirement_key: "req-key-2",
      importance: "must_have",
      text: "Must be eligible to work in Canada",
      evidence_keys: ["evidence-1"],
      span_status: "needs_review",
      established: true,
      unmet: false,
      dimension: "hard",
      is_hard_gate: true,
    },
  ],
};

describe("FitReport", () => {
  it("shows hard-gate review state and opens linked evidence", () => {
    const onShowEvidence = vi.fn();

    render(
      <FitReport
        error={null}
        exportUrl="/fit-report.md"
        isError={false}
        isLoading={false}
        onShowEvidence={onShowEvidence}
        report={report}
      />,
    );

    expect(screen.getByRole("status").textContent).toContain("need review");
    expect(screen.getByText("Must be eligible to work in Canada").textContent).toBe(
      "Must be eligible to work in Canada",
    );
    expect(screen.getByText("Lead a data platform").textContent).toBe("Lead a data platform");
    expect(screen.getByText("evaluation-1").textContent).toBe("evaluation-1");
    expect(document.body.textContent).toContain("Evaluation v2");

    fireEvent.click(screen.getByRole("button", { name: "Evidence evidence" }));
    expect(onShowEvidence).toHaveBeenCalledWith("evidence-1");
  });

  it("marks a preserved report stale after a failed re-evaluation", () => {
    render(
      <FitReport
        error={null}
        exportUrl="/fit-report.md"
        isError={false}
        isLoading={false}
        onShowEvidence={vi.fn()}
        report={report}
        staleReason="The latest re-evaluation failed. This report was not replaced."
      />,
    );

    expect(screen.getByText(/This report was not replaced/).textContent).toContain(
      "was not replaced",
    );
    expect(screen.getByText("evaluation-1")).toBeTruthy();
  });

  it("says a requirement the run never settled is not a gap in the CV", () => {
    const unsettled: FitReportData = {
      ...report,
      requirements: [{ ...report.requirements[0]!, established: false }],
    };

    render(
      <FitReport
        error={null}
        exportUrl="/fit-report.md"
        isError={false}
        isLoading={false}
        onShowEvidence={vi.fn()}
        report={unsettled}
      />,
    );

    const notice = screen.getByRole("status").textContent ?? "";
    expect(notice).toContain("did not establish 1 requirement(s)");
    expect(notice).toContain("not gaps in the master CV");
    expect(screen.getByText("Not established")).toBeTruthy();
  });

  it("asks the person to decide an unestablished gate no requirement restates", () => {
    const unpairedGate: FitReportData = {
      ...report,
      requirements: [report.requirements[0]!, { ...report.requirements[1]!, established: false }],
    };

    render(
      <FitReport
        error={null}
        exportUrl="/fit-report.md"
        isError={false}
        isLoading={false}
        onShowEvidence={vi.fn()}
        report={unpairedGate}
      />,
    );

    const notices = screen.getAllByRole("status").map((node) => node.textContent ?? "");
    const gate = notices.find((text) => text.includes("hard gate(s)")) ?? "";
    expect(gate).toContain("did not settle 1 hard gate(s) either way");
    expect(gate).toContain("decide whether you meet it");
    expect(notices.join(" ")).not.toMatch(/re-run/i);
  });

  it("offers a re-run for a gate whose restating requirement is unestablished", () => {
    const unsettledBoth: FitReportData = {
      ...report,
      requirements: [
        { ...report.requirements[0]!, established: false },
        { ...report.requirements[1]!, established: false },
      ],
    };

    render(
      <FitReport
        error={null}
        exportUrl="/fit-report.md"
        isError={false}
        isLoading={false}
        onShowEvidence={vi.fn()}
        report={unsettledBoth}
      />,
    );

    const notices = screen.getAllByRole("status").map((node) => node.textContent ?? "");
    const rerun = notices.find((text) => text.includes("Re-run the evaluation")) ?? "";
    const gate = notices.find((text) => text.includes("hard gate(s)")) ?? "";
    expect(rerun).toContain("did not establish 1 requirement(s) either way");
    expect(gate).toContain("re-running the evaluation may settle both");
  });

  it("marks a settled but unanswered requirement as unmet", () => {
    const unmet: FitReportData = {
      ...report,
      requirements: [{ ...report.requirements[0]!, established: true, unmet: true }],
    };

    render(
      <FitReport
        error={null}
        exportUrl="/fit-report.md"
        isError={false}
        isLoading={false}
        onShowEvidence={vi.fn()}
        report={unmet}
      />,
    );

    expect(screen.getByText("Unmet")).toBeTruthy();
    expect(screen.queryByText("Not established")).toBeNull();
  });

  it("marks nothing when every requirement was settled", () => {
    render(
      <FitReport
        error={null}
        exportUrl="/fit-report.md"
        isError={false}
        isLoading={false}
        onShowEvidence={vi.fn()}
        report={{ ...report, requirements: [report.requirements[0]!] }}
      />,
    );

    expect(screen.queryByText("Not established")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
