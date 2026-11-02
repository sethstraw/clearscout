import { useState } from "react";

import type { PendingDiscovery } from "../../api/client";
import { useAttachPendingPostingText, useConfirmPendingPostingIdentity } from "../../api/hooks";
import { safeHttpUrl } from "../../api/safeUrl";
import { ErrorState } from "../../components/States";
import { postingLabel } from "./fallbacks";
import { AssistedRecoveryAction } from "./AssistedRecoveryAction";
import { PublisherFactsRefresh } from "./PublisherFactsRefresh";
import {
  IDENTITY_REASONS,
  contentNeedsRecovery,
  contentReadiness,
} from "./pendingDiscoveryFilterModel";

/** Give every non-ready posting an immediate, non-destructive remediation path. */
export function PendingPostingRemediation({
  item,
  workspaceId,
}: {
  item: PendingDiscovery;
  workspaceId: string;
}) {
  const readiness = contentReadiness(item);
  if (readiness === "ready") return null;
  return (
    <PendingPostingRemediationControls
      item={item}
      readiness={readiness}
      workspaceId={workspaceId}
    />
  );
}

function PendingPostingRemediationControls({
  item,
  readiness,
  workspaceId,
}: {
  item: PendingDiscovery;
  readiness: ReturnType<typeof contentReadiness>;
  workspaceId: string;
}) {
  const attach = useAttachPendingPostingText(workspaceId);
  const confirmIdentity = useConfirmPendingPostingIdentity(workspaceId);
  const [open, setOpen] = useState(false);
  const [content, setContent] = useState("");
  const [attested, setAttested] = useState(false);
  const identityReview = IDENTITY_REASONS.has(readiness);
  const itemLabel = postingLabel(
    item.title,
    item.employer,
    safeHttpUrl(item.canonical_url || item.discovery_url),
  );
  const attachmentId = `posting-text-${item.id}`;
  const usefulContent = content.trim();
  const expectedUrl = item.canonical_url || item.discovery_url;

  const submitText = () => {
    if (!attested || usefulContent.length < 400 || usefulContent.length > 120_000) return;
    attach.mutate(
      {
        pendingId: item.id,
        body: {
          content: usefulContent,
          attested_current_exact_posting: true,
          expected_current_content_id: item.current_content_id ?? null,
          expected_url: expectedUrl,
        },
      },
      {
        onSuccess: () => {
          setContent("");
          setAttested(false);
          setOpen(false);
        },
      },
    );
  };

  return (
    <div className="pending-posting-remediation">
      {readiness === "partial_text" ? (
        <p className="field-note">
          Only part of this posting was read; the text appears cut off. Attach the full posting text
          or find the employer&apos;s page before a full evaluation.
        </p>
      ) : null}
      <PublisherFactsRefresh
        itemLabel={itemLabel}
        pendingDiscoveryIds={[item.id]}
        workspaceId={workspaceId}
      />
      {contentNeedsRecovery(item) ? (
        <AssistedRecoveryAction
          itemLabel={itemLabel}
          pendingDiscoveryIds={[item.id]}
          workspaceId={workspaceId}
        />
      ) : null}
      {identityReview ? (
        <div>
          <p className="field-note">
            {item.observed_publisher_title || item.observed_publisher_employer
              ? `Retained publisher evidence names ${item.observed_publisher_title || "this role"}${item.observed_publisher_employer ? ` at ${item.observed_publisher_employer}` : ""}.`
              : "The retained publisher body could not be matched confidently to this row."}{" "}
            Confirm only if it is the same posting shown by the link.
          </p>
          <button
            aria-label={`Confirm same posting for ${itemLabel}`}
            className="button button--quiet"
            disabled={confirmIdentity.isPending || !item.current_content_id}
            onClick={() =>
              confirmIdentity.mutate({
                pendingId: item.id,
                body: {
                  attested_same_posting: true,
                  expected_current_content_id: item.current_content_id ?? "",
                  expected_url: expectedUrl,
                },
              })
            }
            type="button"
          >
            {confirmIdentity.isPending ? "Confirming identity..." : "Confirm same posting"}
          </button>
        </div>
      ) : null}
      <button
        aria-controls={attachmentId}
        aria-expanded={open}
        aria-label={`${open ? "Cancel text attachment" : "Attach posting text"} for ${itemLabel}`}
        className="button button--quiet"
        onClick={() => setOpen((current) => !current)}
        type="button"
      >
        {open ? "Cancel text attachment" : "Attach posting text"}
      </button>
      {open ? (
        <div id={attachmentId}>
          <label>
            Current posting text for {itemLabel}
            <textarea
              maxLength={120_000}
              minLength={400}
              onChange={(event) => setContent(event.target.value)}
              rows={10}
              value={content}
            />
          </label>
          <p className="field-note">Paste 400 to 120,000 characters from the current posting.</p>
          <label>
            <input
              checked={attested}
              onChange={(event) => setAttested(event.target.checked)}
              type="checkbox"
            />{" "}
            I obtained this text from the current posting at the link in this row.
          </label>
          <button
            className="button button--primary"
            disabled={
              !attested ||
              usefulContent.length < 400 ||
              usefulContent.length > 120_000 ||
              attach.isPending
            }
            onClick={submitText}
            type="button"
          >
            {attach.isPending ? "Attaching text..." : "Use this posting text"}
          </button>
        </div>
      ) : null}
      {attach.isError ? <ErrorState error={attach.error} /> : null}
      {confirmIdentity.isError ? <ErrorState error={confirmIdentity.error} /> : null}
    </div>
  );
}
