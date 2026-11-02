import type { SearchParametersState } from "../../api/client/types";

/** The saved search parameters, unconfigured unless a test says otherwise. */
export function searchParametersState(
  overrides: Partial<SearchParametersState> = {},
): SearchParametersState {
  return {
    workspace_id: "workspace-1",
    id: null,
    version: 0,
    configured: false,
    content_sha256: "",
    effective_at: null,
    parameters: {
      opportunity_thesis: "",
      score_weight_set: "",
      working_day_shift_hours: 0,
      locations: [],
    },
    ...overrides,
  };
}
