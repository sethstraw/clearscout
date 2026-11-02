/** The visible credit an aggregator's terms ask for beside its data. */
export type AggregatorCredit = { name: string; href: string };

type CreditRule = {
  host: string;
  name: string;
  /** The credit link; null links back to the row's own address. */
  link: string | null;
};

// Discovery rows carry no source or platform on the wire, so the host names the aggregator.
const CREDIT_RULES: readonly CreditRule[] = [
  { host: "himalayas.app", name: "Himalayas", link: null },
  { host: "arbeitnow.com", name: "Arbeitnow", link: "https://www.arbeitnow.com/" },
];

function creditRule(value: string | null): CreditRule | null {
  if (!value) return null;
  let hostname: string;
  try {
    hostname = new URL(value).hostname;
  } catch {
    return null;
  }
  return (
    CREDIT_RULES.find((rule) => hostname === rule.host || hostname.endsWith(`.${rule.host}`)) ??
    null
  );
}

/** The credit owed for a posting at this address, or null when no aggregator listed it. */
export function aggregatorCredit(url: string | null): AggregatorCredit | null {
  const rule = creditRule(url);
  if (!rule || !url) return null;
  return { name: rule.name, href: rule.link ?? url };
}

/** The address a staged posting's links open; Himalayas requires its applicationLink. */
export function postingAddress(posting: { canonical_url: string; discovery_url: string }): string {
  if (creditRule(posting.discovery_url)?.name === "Himalayas") return posting.discovery_url;
  return posting.canonical_url || posting.discovery_url;
}
