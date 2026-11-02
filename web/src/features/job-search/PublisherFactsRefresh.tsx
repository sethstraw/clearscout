import { useConfirmation } from "../../confirmations";
import { usePostingRefresh } from "./usePostingRefresh";

/** Re-read selected pending discoveries from their publishers without a provider call. */
export function PublisherFactsRefresh({
  itemLabel,
  pendingDiscoveryIds,
  workspaceId,
}: {
  itemLabel?: string;
  pendingDiscoveryIds: string[];
  workspaceId: string;
}) {
  const confirm = useConfirmation();
  const refresh = usePostingRefresh(workspaceId);
  const selected = pendingDiscoveryIds.length;
  const status = refresh.status;
  const withdrawn = refresh.count("withdrawn_as_closed");
  const unverified = refresh.count("unverified");
  const notCheckable = refresh.count("not_checkable");
  const failed = refresh.count("failed");
  const unreadable = refresh.count("content_unreadable");

  const refreshSelection = async () => {
    const approved = await confirm({
      title: selected === 1 ? "Refresh this posting?" : `Refresh ${selected} selected postings?`,
      description:
        selected === 1
          ? "Re-reads this posting from its publisher. If the publisher no longer lists it, it is closed, and a link that cannot be read keeps its stored facts and your corrected title. Nothing is spent."
          : `Re-reads all ${selected} from their publishers, which takes a few minutes and can be cancelled from Tasks. A posting its publisher no longer lists is closed, and a link that cannot be read keeps its stored facts and your corrected title. Nothing is spent.`,
      confirmLabel: selected === 1 ? "Refresh" : `Refresh ${selected}`,
    });
    if (!approved) return;
    try {
      await refresh.refresh(pendingDiscoveryIds);
    } catch {
      // The enqueue error is surfaced in the adjacent alert.
    }
  };

  return (
    <div className="publisher-facts-refresh">
      <button
        aria-label={itemLabel ? `Refresh postings for ${itemLabel}` : undefined}
        className="button button--quiet"
        disabled={selected === 0 || refresh.running}
        onClick={() => void refreshSelection()}
        type="button"
      >
        {refresh.running
          ? "Refreshing postings..."
          : selected === 1
            ? "Refresh posting"
            : `Refresh postings (${selected})`}
      </button>
      <span className="field-note">Free. Re-reads facts, liveness, and posting text.</span>
      {status === "completed" && (
        <span className="field-note" role="status">
          Refreshed {refresh.count("refreshed")}: {refresh.count("content_readable")} readable
          {unreadable > 0 ? `, ${unreadable} unreadable` : ""}
          {withdrawn > 0 ? `, ${withdrawn} closed` : ""}
          {unverified > 0 ? `, ${unverified} could not verify` : ""}
          {notCheckable > 0 ? `, ${notCheckable} not checkable` : ""}
          {failed > 0 ? `, ${failed} failed` : ""}.
          {withdrawn > 0
            ? " A withdrawn row is no longer listed by its publisher; find it in Closed posting history."
            : ""}
        </span>
      )}
      {status === "failed" && (
        <span className="field-error" role="alert">
          Posting refresh failed: {refresh.failureMessage || "No failure detail was returned."}
        </span>
      )}
      {status === "cancelled" && (
        <span className="field-note" role="status">
          Posting refresh was cancelled. No unstarted row was re-read.
        </span>
      )}
      {refresh.error && (
        <span className="field-error" role="alert">
          Could not refresh postings: {refresh.error.message}
        </span>
      )}
    </div>
  );
}
