import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import type { EvaluationPreflight } from "../api/client";
import { EvaluationCostDetails } from "./EvaluationCostDetails";

it("explains missing stage estimates without presenting unknown costs as free", () => {
  const estimate = {
    workspace_id: "workspace-1",
    currency: "USD",
    advisory: true,
    generated_at: "2026-10-08T12:00:00Z",
    basis: "Retained local inputs.",
    limitations: [],
    items: [
      {
        target_kind: "job",
        target_id: "job-1",
        stages: [
          {
            stage: "jobspec",
            provider: "openai",
            model: "configured-model",
            requested_service_tier: "flex",
            price: null,
            provenance: null,
            input_token_estimate: 100,
            input_token_range_high: 400,
            output_token_budget: 500,
            historical_output_samples: 0,
            expected_unavailable_reason: "No usable paid output-token samples.",
            range_unavailable_reason: "No current model price.",
          },
        ],
      },
    ],
  } satisfies EvaluationPreflight;
  render(<EvaluationCostDetails estimate={estimate} />);
  expect(screen.getByText("No usable paid output-token samples.")).toBeTruthy();
  expect(screen.getByText("No current model price.")).toBeTruthy();
  expect(screen.getByText(/Estimated total at configured rates/).textContent).toContain(
    "Unavailable",
  );
  expect(screen.queryByText(/\$0\.0000/)).toBeNull();
  expect(screen.getByText(/Historical output samples/).textContent).toContain(
    "Output-token budget: 500",
  );
});

it("shows the one date of the price table a stage was priced from", () => {
  const estimate = {
    workspace_id: "workspace-1",
    currency: "USD",
    advisory: true,
    generated_at: "2026-10-08T12:00:00Z",
    basis: "Retained local inputs.",
    limitations: [],
    items: [
      {
        target_kind: "job",
        target_id: "job-1",
        stages: [
          {
            stage: "match",
            provider: "openai",
            model: "configured-model",
            requested_service_tier: "flex",
            price: null,
            provenance: {
              source_kind: "bundled_catalog",
              source_urls: ["https://example.com/pricing"],
              snapshot_label: "synthetic-catalog 1.0",
              currency: "USD",
              table_date: "2026-09-25",
              table_date_unavailable_reason: "",
              overridden_fields: [],
            },
            input_token_estimate: 100,
            input_token_range_high: 400,
            output_token_budget: 500,
            historical_output_samples: 0,
          },
        ],
      },
    ],
  } satisfies EvaluationPreflight;
  render(<EvaluationCostDetails estimate={estimate} />);
  expect(screen.getByText(/Price table date/).textContent).toContain("2026-09-25");
});
