import type { PendingDiscovery } from "../../api/client";
import { StatusBadge } from "../../components/StatusBadge";
import { DiscoveryScreeningDetails } from "./DiscoveryScreeningDetails";

export function PendingDiscoveryProvenance({ discovery }: { discovery: PendingDiscovery }) {
  const freshness = discovery.staged_facts?.freshness;
  return (
    <div className="pending-provenance">
      <div className="pending-provenance__facts">
        <span className="badge">First seen {discovery.first_seen_on}</span>
        <span className="badge">Rank {discovery.rank ?? "not stated"}</span>
        {discovery.matched_target_title ? <span className="badge">Matched target</span> : null}
        {freshness && freshness !== "unknown" ? <StatusBadge value={freshness} /> : null}
      </div>
      {freshness === "likely_stale" ? (
        <p className="field-note">
          The publisher&apos;s own expiry date for this posting has passed. It has not been checked
          for closure.
        </p>
      ) : null}
      {freshness === "verified_current" ? (
        <p className="field-note">
          The board no longer lists this posting, but a later read of the posting itself found it
          open.
        </p>
      ) : null}
      <details className="pending-provenance__details">
        <summary>{discovery.reason}</summary>
        {discovery.matched_target_title ? (
          <p className="field-note">Matched target title: {discovery.matched_target_title}.</p>
        ) : null}
        {(discovery.ranked_because ?? []).length > 0 ? (
          <p className="field-note">
            Ranked because: {(discovery.ranked_because ?? []).join(", ")}.
          </p>
        ) : null}
      </details>
      <DiscoveryScreeningDetails discovery={discovery} />
    </div>
  );
}
