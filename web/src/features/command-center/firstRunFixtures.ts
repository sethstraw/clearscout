import { HttpResponse, http, type HttpHandler } from "msw/http";

import type { MasterCvVersion, Source } from "../../api/client/types";
import { sourceList } from "../job-search/sourceScanFixtures";
import { searchParametersState } from "../search-parameters/searchParameterFixtures";

const API = "*/api/v1";
const WORKSPACE = `${API}/workspaces/:workspaceId`;

const CV_VERSION = {
  id: "cv-1",
  content_sha256: "c".repeat(64),
  effective_at: "2026-10-01T12:00:00Z",
  is_current: true,
  source_uri: "file:///synthetic/cv.md",
  byte_size: 1200,
} satisfies MasterCvVersion;

export interface FirstRunSetup {
  cv?: boolean;
  titles?: string[];
  boards?: Source[];
}

/** The reads behind the Home setup checklist, at a given point in setup. */
export function firstRunHandlers({
  cv = false,
  titles = [],
  boards = [],
}: FirstRunSetup = {}): HttpHandler[] {
  return [
    http.get(`${WORKSPACE}/master-cv/versions`, () => HttpResponse.json(cv ? [CV_VERSION] : [])),
    http.get(`${API}/job-search-parameters`, () =>
      HttpResponse.json(
        searchParametersState({
          parameters: {
            opportunity_thesis: "",
            score_weight_set: "",
            working_day_shift_hours: 0,
            locations: [],
            seniority: {
              minimum: "",
              maximum: "",
              title_matching: "guided",
              target_titles: titles.map((title) => ({ title, lane: null })),
            },
          },
        }),
      ),
    ),
    http.get(`${WORKSPACE}/sources`, () => HttpResponse.json(sourceList(boards))),
  ];
}
