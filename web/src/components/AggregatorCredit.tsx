import { safeHttpUrl } from "../api/safeUrl";
import { aggregatorCredit } from "./aggregatorHosts";

/** A "Job data from <aggregator>" link inside a span, or nothing for a non-aggregator address. */
export function AggregatorCreditLink({ url }: { url: string | null }) {
  const credit = aggregatorCredit(url);
  const href = credit ? safeHttpUrl(credit.href) : null;
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
export function AggregatorCreditNote({ url }: { url: string | null }) {
  if (!aggregatorCredit(url)) return null;
  return (
    <p className="field-note">
      <AggregatorCreditLink url={url} />
    </p>
  );
}
