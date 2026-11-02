import type {
  ScreenedOutTitle,
  ScreenedOutTitlePage,
  Source,
  SourceList,
  SourceScan,
  SourceScanHistory,
  SourceScanOutcomePage,
} from "../../api/client/types";

type SourceScanOutcome = SourceScanOutcomePage["items"][number];

const NO_SKIPS = {
  arrangement_office_limit: 0,
  arrangement_refused: 0,
  bounded_result_cap: 0,
  compensation_below_minimum: 0,
  duplicate_workday_identity: 0,
  incomplete_oracle_listing: 0,
  employer_excluded: 0,
  employment_excluded: 0,
  employment_outside: 0,
  incomplete_workday_listing: 0,
  location_ineligible: 0,
  location_outside: 0,
  missing_required_posting_identity: 0,
  protected_posting: 0,
  repeated_workday_page: 0,
  seniority_outside: 0,
  title_excluded: 0,
  title_mismatch: 0,
  title_role_family_conflict: 0,
  title_role_family_review: 0,
} satisfies SourceScan["skip_reasons"];

const NO_DETAIL_FAILURES = {
  timeout: 0,
  http_status: 0,
  parse: 0,
  other: 0,
} satisfies SourceScan["detail_failure_reasons"];

/** One completed board scan that read two listings and kept one. */
export function sourceScan(overrides: Partial<SourceScan> = {}): SourceScan {
  return {
    id: "scan-1",
    task_id: "task-1",
    source_name: "Acme careers",
    employer: "Acme",
    source_url: "https://jobs.example.test/acme",
    source_type: "employer_board",
    priority: 1,
    enabled: true,
    query_label: "title-filter",
    kind: "title-filter",
    scanned_on: "2026-08-24",
    status: "completed",
    read_outcome: "ats_complete",
    raw_count: 2,
    read_count: 2,
    skip_reasons: NO_SKIPS,
    result_count: 1,
    new_count: 1,
    screened_out_count: 0,
    frontier_pending_count: 0,
    frontier_completed_count: 0,
    frontier_failed_count: 0,
    automatic_pending_count: 0,
    follow_up_state: "none",
    kept_as: "latest_and_complete",
    detail_attempted_count: 0,
    detail_read_count: 0,
    detail_failed_count: 0,
    detail_capped_count: 0,
    detail_failure_reasons: NO_DETAIL_FAILURES,
    notes: "",
    complete: true,
    scan_started_at: "2026-08-24T00:00:00Z",
    scan_completed_at: "2026-08-24T00:00:01Z",
    duration_ms: 1000,
    ...overrides,
  };
}

export function sourceScanSkips(
  overrides: Partial<SourceScan["skip_reasons"]> = {},
): SourceScan["skip_reasons"] {
  return { ...NO_SKIPS, ...overrides };
}

export function sourceScanDetailFailures(
  overrides: Partial<SourceScan["detail_failure_reasons"]> = {},
): SourceScan["detail_failure_reasons"] {
  return { ...NO_DETAIL_FAILURES, ...overrides };
}

/** One enabled board the scan scope reads. */
export function boardSource(overrides: Partial<Source> = {}): Source {
  return {
    id: "source-1",
    name: "Northwind",
    employer: "Northwind",
    enabled: true,
    priority: 1,
    source_type: "employer_board",
    platform: "greenhouse",
    url: "https://jobs.example.test/northwind",
    careers_url: null,
    terms: { condition: "Public board API", url: "" },
    last_status: null,
    last_scanned_on: null,
    last_read_outcome: null,
    last_notes: "",
    scan_count: 0,
    staged_count: 0,
    read_scan_count: 0,
    incomplete_scan_count: 0,
    failed_scan_count: 0,
    first_read_on: null,
    last_listed_on: null,
    ...overrides,
  };
}

export function sourceList(items: Source[]): SourceList {
  return { items };
}

export function sourceScanHistory(items: SourceScan[]): SourceScanHistory {
  return { items, total: items.length, offset: 0, limit: 50 };
}

/** One rejected board listing, the row the screened-candidate table renders. */
export function sourceScanOutcome(overrides: Partial<SourceScanOutcome> = {}): SourceScanOutcome {
  return {
    id: "outcome-1",
    task_id: "task-1",
    source_scan_id: "scan-1",
    source_name: "Acme careers",
    candidate_kind: "board_listing",
    url: "https://acme.example/jobs/one",
    employer: "Acme",
    title: "Data Product Lead",
    location: "Canada",
    terminal_stage: "rejected",
    reason: "excluded_employer",
    detail: "",
    screening_findings: [{ dimension: "location", code: "location_outside", subject: "Berlin" }],
    created_at: "2026-09-04T00:00:00Z",
    ...overrides,
  };
}

export function sourceScanOutcomePage(
  overrides: Partial<SourceScanOutcomePage> = {},
): SourceScanOutcomePage {
  const items = overrides.items ?? [sourceScanOutcome()];
  return {
    items,
    total: items.length,
    offset: 0,
    limit: 100,
    sources: ["Acme careers"],
    terminal_stages: ["rejected"],
    reasons: ["excluded_employer", "staged"],
    candidate_kinds: ["board_listing"],
    capture_state: "complete",
    recorded_outcomes: items.length,
    follow_up_outcomes: 0,
    reported_outcomes: items.length,
    reported_omitted_outcomes: 0,
    ...overrides,
  };
}

export function screenedOutTitle(overrides: Partial<ScreenedOutTitle> = {}): ScreenedOutTitle {
  return {
    id: "title-1",
    url: "https://boards.example.test/acme/jobs/1",
    source_name: "Acme careers",
    employer: "Acme",
    title: "Sales Director",
    reason: "insufficient_title_evidence",
    title_code: "title_mismatch",
    title_subject: "",
    first_seen_at: "2025-03-01T09:00:00Z",
    last_seen_at: "2025-03-02T09:00:00Z",
    reviewed_at: null,
    review_state: "new",
    ...overrides,
  };
}

export function screenedOutTitlePage(
  items: ScreenedOutTitle[],
  overrides: Partial<ScreenedOutTitlePage> = {},
): ScreenedOutTitlePage {
  return {
    items,
    total: items.length,
    offset: 0,
    limit: 100,
    sources: [...new Set(items.map((item) => item.source_name))],
    all_count: items.length,
    needs_review_count: items.filter((item) => item.review_state !== "reviewed").length,
    ...overrides,
  };
}
