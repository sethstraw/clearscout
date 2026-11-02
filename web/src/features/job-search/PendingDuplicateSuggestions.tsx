import type { PendingDuplicateSuggestion } from "../../api/client";
import { safeHttpUrl } from "../../api/safeUrl";
import { EmployerName } from "./EmployerName";
import { postingLabel, titleFallback } from "./fallbacks";

const SIGNAL_LABELS: Record<PendingDuplicateSuggestion["signal"], string> = {
  retained_body_digest: "Same posting text, from a different site",
  title_and_employer: "Same title and employer, from a different site",
  title_and_place_hint: "Same title and place; the employer on one of them is a guess",
};

const KEEPER_REASONS: Record<PendingDuplicateSuggestion["keeper_basis"], string> = {
  in_queue: "the other copy is already dismissed",
  already_evaluated: "it is already evaluated",
  employer_board_source: "it comes from a saved employer board",
  publisher_vouched: "the publisher named the employer",
  relevance_reviewed: "it already has a relevance review",
  retained_body: "it still has the posting text",
  first_seen: "it was found first",
  identifier_order: "nothing else tells the copies apart",
};

/**
 * Lists the found jobs a deterministic signal links to this one, for a person to confirm.
 */
export function PendingDuplicateSuggestions({
  onDismissAsDuplicateOf,
  pendingDiscoveryId,
  suggestions,
}: {
  onDismissAsDuplicateOf: (keeperId: string) => void;
  pendingDiscoveryId: string;
  suggestions: PendingDuplicateSuggestion[];
}) {
  const [first] = suggestions;
  if (!first) return null;
  const keeperId = first.keeper_pending_discovery_id;
  const others = suggestions.length === 1 ? "the found job below" : "the found jobs below";
  return (
    <div className="pending-row-details__group">
      <p className="field-note">
        {keeperId === pendingDiscoveryId
          ? `This may be the same opening as ${others}. Keep this one, because ${KEEPER_REASONS[first.keeper_basis]}. Dismiss the others from their own rows in this queue.`
          : `This may be the same opening as ${others}. The one marked Keep is suggested, because ${KEEPER_REASONS[first.keeper_basis]}.`}{" "}
        Nothing changes until you dismiss a row.
      </p>
      <div className="table-scroll">
        <table aria-label="Possible duplicates of this found job" className="data-table">
          <thead>
            <tr>
              <th scope="col">Employer</th>
              <th scope="col">Role</th>
              <th scope="col">Place</th>
              <th scope="col">Why it matches</th>
              <th scope="col">Status</th>
              <th scope="col">Action</th>
            </tr>
          </thead>
          <tbody>
            {suggestions.map((item) => {
              const url = safeHttpUrl(item.discovery_url);
              const isKeeper = item.pending_discovery_id === keeperId;
              return (
                <tr key={item.pending_discovery_id}>
                  <td>
                    <EmployerName employer={item.employer} url={url} />
                  </td>
                  <td>
                    {url ? (
                      <a
                        aria-label={postingLabel(item.title, item.employer, url)}
                        href={url}
                        rel="noreferrer"
                        target="_blank"
                      >
                        {item.title || titleFallback(url)}
                      </a>
                    ) : (
                      item.title || titleFallback(url)
                    )}
                  </td>
                  <td>{item.location || "Not stated"}</td>
                  <td>{SIGNAL_LABELS[item.signal]}</td>
                  <td>{item.dismissed ? "Dismissed" : isKeeper ? "Keep" : "In the queue"}</td>
                  <td>
                    {/* A guessed employer is too weak to prefill a dismissal from. */}
                    {isKeeper && item.signal !== "title_and_place_hint" ? (
                      <button
                        className="button button--quiet"
                        onClick={() => onDismissAsDuplicateOf(item.pending_discovery_id)}
                        type="button"
                      >
                        Keep this one, dismiss the job you are reviewing
                      </button>
                    ) : (
                      "-"
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
