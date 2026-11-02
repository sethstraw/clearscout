import { useState } from "react";

import { useReconcileSavedClosures } from "../../api/hooks";
import { useConfirmation } from "../../confirmations";

export function SavedClosureAction({
  pendingDiscoveryIds,
  workspaceId,
}: {
  pendingDiscoveryIds: string[];
  workspaceId: string;
}) {
  const confirm = useConfirmation();
  const reconcile = useReconcileSavedClosures(workspaceId);
  const [message, setMessage] = useState("");
  const selected = pendingDiscoveryIds.length;

  const apply = async () => {
    const approved = await confirm({
      title: `Mark ${selected} ${selected === 1 ? "posting" : "postings"} closed from saved publisher evidence?`,
      description:
        "This records the already-saved publisher closure without reading the publisher again. " +
        "The posting leaves active triage and remains in Closed posting history with its evidence. " +
        "Only a later publisher read that reports the posting active can restore it.",
      confirmLabel: "Mark closed from saved evidence",
    });
    if (!approved) return;
    try {
      const response = await reconcile.mutateAsync({ pending_discovery_ids: pendingDiscoveryIds });
      const applied = response.results.filter((result) => result.outcome === "applied").length;
      setMessage(
        applied
          ? `Marked ${applied} ${applied === 1 ? "posting" : "postings"} closed from saved publisher evidence.`
          : "No selected posting has current saved closure evidence to apply.",
      );
    } catch {
      // The mutation error is rendered beside the action.
    }
  };

  if (selected === 0) return null;
  return (
    <div className="publisher-facts-refresh">
      <button
        className="button button--quiet"
        disabled={reconcile.isPending}
        onClick={() => void apply()}
        type="button"
      >
        {reconcile.isPending
          ? "Applying saved closure evidence..."
          : `Mark closed from saved evidence (${selected})`}
      </button>
      <span className="field-note">
        Uses current saved publisher closure evidence. It does not re-read the publisher.
      </span>
      {message && (
        <span className="field-note" role="status">
          {message}
        </span>
      )}
      {reconcile.isError && (
        <span className="field-error" role="alert">
          Could not apply saved closure evidence: {reconcile.error.message}
        </span>
      )}
    </div>
  );
}
