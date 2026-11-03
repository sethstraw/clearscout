import type { AggregatorCredit } from "../../api/client/types";

/** The address a staged posting's links open, which some aggregators' terms fix to their own. */
export function postingAddress(posting: {
  aggregator?: AggregatorCredit | null;
  canonical_url: string;
  discovery_url: string;
}): string {
  if (posting.aggregator?.opens_discovery_url) return posting.discovery_url;
  return posting.canonical_url || posting.discovery_url;
}
