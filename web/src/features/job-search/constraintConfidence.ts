import type { PendingDiscovery } from "../../api/client";

export type ConstraintConfidence = PendingDiscovery["constraint_confidence"];

export const CONSTRAINT_CONFIDENCE_ROUTES: Record<
  ConstraintConfidence,
  { label: string; definition: string }
> = {
  pursue: {
    label: "Pursue",
    definition: "Published facts satisfy the known hard search constraints.",
  },
  investigate: {
    label: "Investigate",
    definition: "A preferred search constraint remains incomplete.",
  },
  needs_verification: {
    label: "Needs verification",
    definition: "An eligibility or location requirement remains unresolved.",
  },
  uncertain_comp: {
    label: "Compensation uncertain",
    definition: "The posting does not resolve a stated compensation constraint.",
  },
  excluded: {
    label: "Excluded",
    definition: "Published facts fail a deterministic hard search constraint.",
  },
};

export function constraintConfidenceRoute(value: unknown) {
  return typeof value === "string" && value in CONSTRAINT_CONFIDENCE_ROUTES
    ? CONSTRAINT_CONFIDENCE_ROUTES[value as ConstraintConfidence]
    : null;
}
