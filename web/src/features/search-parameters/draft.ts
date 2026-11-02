import type { JobSearchParameters } from "../../api/client";

/**
 * A text input cannot hold "absent" and "empty" as different states, so the
 * form works in a fully-populated shape and normalizes once on load.
 */
export type Draft = Required<
  Omit<JobSearchParameters, "arrangement" | "seniority" | "locations">
> & {
  locations: (Omit<NonNullable<JobSearchParameters["locations"]>[number], "eligibility"> & {
    eligibility: NonNullable<JobSearchParameters["locations"]>[number]["eligibility"] | "";
  })[];
  arrangement: Required<NonNullable<JobSearchParameters["arrangement"]>>;
  seniority: Required<NonNullable<JobSearchParameters["seniority"]>>;
  walkthrough?: {
    step: number;
    paused: boolean;
    answers: Partial<Record<"roles" | "places" | "working" | "pay", "stated" | "unrestricted">>;
  };
};

export type SectionProps = {
  draft: Draft;
  patch: (changes: Partial<Draft>) => void;
};

const EMPTY: Draft = {
  locations: [],
  countries: [],
  working_time_zone: null,
  working_day_shift_hours: 0,
  arrangement: {
    remote: null,
    hybrid: null,
    on_site: null,
    maximum_office_days: null,
    maximum_travel_percent: null,
    relocation: null,
    onsite_requires_paid_travel: null,
  },
  compensation: [],
  seniority: {
    minimum: "",
    maximum: "",
    target_titles: [],
    excluded_titles: [],
    specialist_ic_role_nouns: [],
    strong_domain_tokens: [],
    title_matching: "guided",
  },
  employment: { types: [], excluded_types: [], payroll_arrangements: [] },
  employers: { industries: [], excluded_industries: [], excluded_employers: [], sizes: [] },
  focus: { capabilities: [], domains: [], boundaries: [] },
  excluded_conditions: [],
  score_weight_set: "",
  opportunity_thesis: "",
};

export function normalize(parameters: JobSearchParameters | undefined): Draft {
  if (!parameters) return EMPTY;
  return {
    locations: (parameters.locations ?? []).map((location) => ({
      ...location,
      resident: location.resident ?? false,
    })),
    // The form offers no control for the country table, so a save carries it through.
    countries: parameters.countries ?? [],
    working_time_zone: parameters.working_time_zone ?? null,
    working_day_shift_hours: parameters.working_day_shift_hours ?? 0,
    arrangement: { ...EMPTY.arrangement, ...(parameters.arrangement ?? {}) },
    compensation: parameters.compensation ?? [],
    seniority: { ...EMPTY.seniority, ...(parameters.seniority ?? {}) },
    employment: { ...EMPTY.employment, ...(parameters.employment ?? {}) },
    employers: { ...EMPTY.employers, ...(parameters.employers ?? {}) },
    focus: { ...EMPTY.focus, ...(parameters.focus ?? {}) },
    excluded_conditions: parameters.excluded_conditions ?? [],
    score_weight_set: parameters.score_weight_set ?? "",
    opportunity_thesis: parameters.opportunity_thesis ?? "",
  };
}

/** Removes browser-only progress and requires a deliberate standing for each place. */
export function parametersForSave(draft: Draft): JobSearchParameters {
  const parameters = { ...draft };
  delete parameters.walkthrough;
  return {
    ...parameters,
    locations: draft.locations.map((place) => {
      if (!place.eligibility) throw new Error("Choose your work authorization for each place.");
      return { ...place, eligibility: place.eligibility };
    }),
  };
}

/** A readable place name independent of the stored identifier. */
export function placeLabel(place: Draft["locations"][number]): string {
  return [place.metro, place.region, place.country].filter(Boolean).join(", ") || "Unnamed place";
}

export function asList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function numberOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

/** The message for a value outside a whole-number range, or null when it fits or is unset. */
function wholeNumberError(value: number | null | undefined, max: number): string | null {
  if (value === null || value === undefined) return null;
  return Number.isInteger(value) && value >= 0 && value <= max
    ? null
    : `Enter a whole number from 0 to ${max}.`;
}

/** Whether the draft accepts any office attendance, which is when office days are asked. */
export function wantsOffice(draft: Draft): boolean {
  return draft.arrangement.hybrid !== null || draft.arrangement.on_site !== null;
}

/** Each shown working-step number's error, keyed by the field, null where the value fits. */
export function workingErrors(draft: Draft) {
  return {
    shift: wholeNumberError(draft.working_day_shift_hours, 12),
    officeDays: wantsOffice(draft)
      ? wholeNumberError(draft.arrangement.maximum_office_days, 7)
      : null,
    travel: wholeNumberError(draft.arrangement.maximum_travel_percent, 100),
  };
}

/** Whether any number on the draft would be refused by the server's bounds. */
export function hasNumberErrors(draft: Draft): boolean {
  return Object.values(workingErrors(draft)).some((error) => error !== null);
}
