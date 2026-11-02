import { useEffect, useRef, useState, type ReactNode } from "react";

import type { DispositionSelection } from "../../api/client";
import { useApplyPendingDisposition, usePreviewPendingDisposition } from "../../api/hooks";
import { useConfirmation } from "../../confirmations";
import {
  captureFocusOrigin,
  focusIsParked,
  returnFocus,
  type FocusOrigin,
} from "../../providers/focusReturn";
import { titleFallback } from "./fallbacks";

/** Say what can undo the disposition; a restore cannot reach a row its source no longer lists. */
function permanence(clearsMarkers: boolean, unlisted: boolean): string {
  if (unlisted) {
    return `${clearsMarkers ? "The markers it clears do not come back. " : ""}A discovery its source no longer lists stays dismissed until its source lists it again.`;
  }
  return clearsMarkers
    ? "The disposition is reversible; the markers it clears are not."
    : "This changes only the reversible pending-list disposition.";
}

/**
 * Preview, confirm, and apply one reversible pending-list disposition. Shared by
 * the dispositions panel and the queue's keyboard triage.
 */
export function useDispositionAction(workspaceId: string, neverRead?: ReadonlySet<string>) {
  const preview = usePreviewPendingDisposition(workspaceId);
  const apply = useApplyPendingDisposition(workspaceId);
  const confirm = useConfirmation();
  // Favorite and kept rows an unconfirmed dismissal left out, from the last applied preview.
  const [skippedMarked, setSkippedMarked] = useState(0);
  // The trigger disables itself while the action runs, so a browser drops its focus to
  // the body; focus goes back once the action settles and the trigger is enabled again.
  const settledFrom = useRef<FocusOrigin | null>(null);
  const [settledCount, setSettledCount] = useState(0);
  const pending = preview.isPending || apply.isPending;
  useEffect(() => {
    const started = settledFrom.current;
    if (!started || pending) return;
    settledFrom.current = null;
    if (focusIsParked(started)) returnFocus(started);
  }, [pending, settledCount]);
  // skipConfirm previews and applies without the modal, for callers that carry
  // their own safety net (the keyboard triage's undo affordance).
  const act = async (
    selection: DispositionSelection,
    options?: {
      skipConfirm?: boolean;
      details?: ReactNode;
      confirmLabel?: string;
      onError?: (error: unknown) => void;
    },
  ): Promise<{ skippedMarked: number } | false> => {
    const startedFrom = captureFocusOrigin();
    try {
      setSkippedMarked(0);
      const exact = await preview.mutateAsync(selection);
      const skipped = exact.skipped_marked_ids?.length ?? 0;
      const withdrawn = exact.skipped_withdrawn_ids?.length ?? 0;
      const clearsMarkers = "dismiss_marked" in selection && selection.dismiss_marked === true;
      if (!options?.skipConfirm) {
        const reason = /[.!?]$/.test(exact.reason) ? exact.reason : `${exact.reason}.`;
        // A regional variant is only ever read through its primary, so it is unread with it.
        const unreadCount =
          selection.action === "dismiss" && neverRead
            ? exact.targets.filter((target) => neverRead.has(target.primary_id)).length
            : 0;
        const evidenceUnavailable =
          selection.action === "dismiss" &&
          selection.selection_mode === "explicit" &&
          selection.category === "current_evidence_unavailable";
        const unlisted =
          selection.action === "dismiss" && exact.targets.some((target) => target.withdrawn_at);
        const approved = await confirm({
          title: `${selection.action === "dismiss" ? "Dismiss" : "Restore"} ${exact.targets.length} ${exact.targets.length === 1 ? "discovery" : "discoveries"}?`,
          description: `Review every affected job below. Reason: ${reason} ${permanence(
            clearsMarkers,
            unlisted,
          )}`,
          details: (
            <div>
              {clearsMarkers && (
                <p className="callout">
                  This includes a favorite or kept discovery. Dismissing it clears its markers: the
                  favorite, the keep marker, and the keep note. Restoring or undoing the dismissal
                  does not bring them back.
                </p>
              )}
              {skipped > 0 && (
                <p className="callout">
                  {skipped} favorite or kept {skipped === 1 ? "discovery is" : "discoveries are"}{" "}
                  left out. Dismiss those from their own rows.
                </p>
              )}
              {withdrawn > 0 && (
                <p className="callout">
                  {withdrawn === 1
                    ? "1 discovery is no longer listed by its source and stays dismissed."
                    : `${withdrawn} discoveries are no longer listed by their source and stay dismissed.`}
                </p>
              )}
              {unreadCount > 0 && (
                <p className="callout">
                  {unreadCount} of these {exact.targets.length} were never read from the publisher.
                  Dismissing them now records the disposition on board metadata alone.
                </p>
              )}
              {evidenceUnavailable && (
                <p className="callout">
                  No suitability decision was made for these postings. This records only that
                  trustworthy current posting evidence remained unavailable. Every affected
                  discovery can be restored manually.
                </p>
              )}
              {selection.action === "restore" && (
                <p className="callout">
                  Restoring returns these to the pending queue but reads nothing. To read a restored
                  posting, select it and choose Refresh postings or Full evaluation.
                </p>
              )}
              {options?.details}
              <p>
                <strong>Exact affected jobs ({exact.targets.length})</strong>
              </p>
              <ol aria-label="Exact affected jobs" className="confirmation-target-list">
                {exact.targets.map((target) => (
                  <li key={target.id}>
                    {target.title || titleFallback(target.discovery_url)}
                    {target.employer ? ` at ${target.employer}` : ""}
                    {target.location ? ` (${target.location})` : ""}
                    {target.kind === "regional_variant" ? " [regional variant]" : ""}
                  </li>
                ))}
              </ol>
            </div>
          ),
          confirmLabel:
            options?.confirmLabel ??
            (selection.action === "dismiss" ? "Dismiss discoveries" : "Restore discoveries"),
        });
        if (!approved) return false;
      }
      await apply.mutateAsync({ ...selection, snapshot_sha256: exact.snapshot_sha256 });
      setSkippedMarked(skipped);
      return { skippedMarked: skipped };
    } catch (error) {
      options?.onError?.(error);
      return false;
    } finally {
      settledFrom.current = startedFrom;
      setSettledCount((count) => count + 1);
    }
  };
  return {
    act,
    error: preview.error ?? apply.error,
    pending,
    skippedMarked,
  };
}
