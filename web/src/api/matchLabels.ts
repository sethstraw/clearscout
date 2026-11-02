/** Human-readable labels for stored match classifications. */
type MatchLabel = {
  label: string;
  meaning: string;
};

const MATCH_LABELS: Record<string, MatchLabel> = {
  DIRECT: {
    label: "Directly covered",
    meaning: "Your record already shows this in language a reviewer will recognise.",
  },
  TAILORING_ONLY: {
    label: "Covered, worded differently",
    meaning:
      "You have done this, but your record calls it something else. Rewording surfaces it without changing the claim.",
  },
  TRANSFERABLE: {
    label: "Related, not the same",
    meaning:
      "Your record is close and credible, but differs in tool, domain, scale, or context. It argues transfer, not possession.",
  },
  ASSERTED: {
    label: "A fact you state",
    meaning:
      "This asks about status, eligibility, or location, and is answered by something you state rather than by any accomplishment.",
  },
  GAP: {
    label: "Not covered",
    meaning: "Nothing in your record supports this requirement.",
  },
};

export function matchLabel(classification: string): string {
  return MATCH_LABELS[classification]?.label ?? classification.replaceAll("_", " ").toLowerCase();
}

export function matchMeaning(classification: string): string {
  return MATCH_LABELS[classification]?.meaning ?? "";
}

/** The classifications present in a set, for a legend that explains only those. */
export function matchLegend(
  classifications: string[],
): { key: string; label: string; meaning: string }[] {
  return [...new Set(classifications)]
    .filter((key) => key in MATCH_LABELS)
    .map((key) => ({ key, label: matchLabel(key), meaning: matchMeaning(key) }));
}
