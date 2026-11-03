import type { AggregatorCredit } from "../api/client/types";
import { safeHttpUrl } from "../api/safeUrl";

type Credit = AggregatorCredit | null | undefined;

/** A "Job data from <aggregator>" link inside a span, or nothing when no aggregator listed the row. */
export function AggregatorCreditLink({ credit }: { credit: Credit }) {
  const href = credit ? safeHttpUrl(credit.credit_url) : null;
  if (!credit || !href) return null;
  return (
    <span>
      <a href={href} rel="noreferrer" target="_blank">
        Job data from {credit.name}
      </a>
    </span>
  );
}

/** The credit as its own note line under a table cell's posting link. */
export function AggregatorCreditNote({ credit }: { credit: Credit }) {
  if (!credit) return null;
  return (
    <p className="field-note">
      <AggregatorCreditLink credit={credit} />
    </p>
  );
}
