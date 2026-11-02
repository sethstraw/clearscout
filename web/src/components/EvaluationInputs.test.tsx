import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Job } from "../api/client";
import { EvaluationInputs } from "./EvaluationInputs";

describe("EvaluationInputs", () => {
  it.each([undefined, []] satisfies Job["evaluation_inputs"][])(
    "labels an unevaluated job without claiming a historical score",
    (inputs) => {
      render(<EvaluationInputs evaluated={false} inputs={inputs} />);

      expect(screen.getByText("Not evaluated")).toBeTruthy();
      expect(screen.queryByText(/Historical score/)).toBeNull();
    },
  );

  it("does not describe saved scores or input comparisons before an evaluation exists", () => {
    render(<EvaluationInputs evaluated={false} inputs={[]} detail />);

    expect(screen.getByText("Not evaluated")).toBeTruthy();
    expect(screen.queryByText(/saved score|Recorded:/)).toBeNull();
  });

  it.each([undefined, []] satisfies Job["evaluation_inputs"][])(
    "retains the provenance warning for a saved evaluation without input identities",
    (inputs) => {
      render(<EvaluationInputs evaluated inputs={inputs} />);

      expect(screen.getByText("Historical score: input identities unknown")).toBeTruthy();
    },
  );

  it("retains changed inputs and their recorded identities for a saved evaluation", () => {
    render(
      <EvaluationInputs
        evaluated
        inputs={[{ name: "cv", recorded: "cv-old", current: "cv-new", status: "changed" }]}
        detail
      />,
    );

    expect(
      screen.getByText(
        "Historical score: CV changed. A saved score is never recalculated by viewing it.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Recorded: cv-old. Current: cv-new.")).toBeTruthy();
  });

  it("identifies a saved evaluation with current inputs", () => {
    render(
      <EvaluationInputs
        evaluated
        inputs={[{ name: "cv", recorded: "cv-1", current: "cv-1", status: "current" }]}
      />,
    );

    expect(screen.getByText("Assessed with current inputs")).toBeTruthy();
  });
});
