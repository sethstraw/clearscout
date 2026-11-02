import type { PendingDuplicateSuggestion } from "../../api/client";

/** Whether any suggestion rests on equal posting text, the only signal worth a chip. */
export function sharesPostingText(suggestions: PendingDuplicateSuggestion[]): boolean {
  return suggestions.some((item) => item.signal === "retained_body_digest");
}

/** How many open copies suggest this found job is the one to dismiss rather than keep. */
export function copiesWorthKeepingInstead(
  pendingDiscoveryId: string,
  suggestions: PendingDuplicateSuggestion[],
): number {
  return suggestions.filter(
    (item) =>
      !item.dismissed &&
      item.signal !== "title_and_place_hint" &&
      item.keeper_pending_discovery_id !== pendingDiscoveryId,
  ).length;
}
