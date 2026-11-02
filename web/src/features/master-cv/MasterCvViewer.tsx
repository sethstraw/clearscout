import { useMemo, useState } from "react";

import type { MasterCvCitationImpact, MasterCvImpact, MasterCvProposal } from "../../api/client";
import {
  MASTER_CV_REREAD,
  useMasterCv,
  useMasterCvDiff,
  useMasterCvImpact,
  useMasterCvRereadCosts,
  useMasterCvVersions,
  useSaveMasterCv,
} from "../../api/hooks";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { PaidAction } from "../../components/PaidAction";
import { unifiedDiff } from "../../components/diff";
import { MarkdownView } from "../../components/MarkdownView";
import { MarkdownWorkspace } from "../../components/MarkdownWorkspace";
import { ErrorState, LoadingState } from "../../components/States";
import { UnifiedDiff } from "../../components/UnifiedDiff";

type DriftGroup = "material" | "changed" | "unchanged" | "missing";

function driftGroup(citation: MasterCvCitationImpact): DriftGroup {
  if (!citation.drift.found) return "missing";
  if (citation.drift.material) return "material";
  return citation.drift.changed ? "changed" : "unchanged";
}

const GROUP_HEADINGS: { group: DriftGroup; heading: string }[] = [
  { group: "material", heading: "Material" },
  { group: "missing", heading: "No longer present" },
  { group: "changed", heading: "Changed wording" },
  { group: "unchanged", heading: "Unchanged" },
];

/** What a review found, grouped so the consequential rows are read first. */
function ImpactReport({ impact }: { impact: MasterCvImpact }) {
  const grouped = useMemo(() => {
    const groups = new Map<DriftGroup, MasterCvCitationImpact[]>();
    for (const citation of impact.citations) {
      const group = driftGroup(citation);
      groups.set(group, [...(groups.get(group) ?? []), citation]);
    }
    return groups;
  }, [impact.citations]);

  return (
    <section>
      <p role="status">
        {impact.citation_count} committed citation{impact.citation_count === 1 ? "" : "s"} read this
        CV. {impact.material_count} material, {impact.changed_count} changed,{" "}
        {impact.unchanged_count} unchanged, {impact.not_found_count} no longer present.
      </p>
      <p className="field-note">
        Material means a figure or a negation moved, so a claim already made against this CV could
        now be wrong; changed means the wording moved and the substance did not.
      </p>
      {impact.omitted_count > 0 && (
        <p className="field-note">
          Listing {impact.returned_count} of {impact.citation_count}. {impact.omitted_count} more
          are not shown here, so this list is not the whole of what an edit touches.
        </p>
      )}
      {impact.citations.length === 0 && (
        <p className="field-note">No committed citation reads any text this edit would change.</p>
      )}
      {GROUP_HEADINGS.map(({ group, heading }) => {
        const rows = grouped.get(group) ?? [];
        if (!rows.length) return null;
        return (
          <div key={group}>
            <h4>
              {heading} ({rows.length})
            </h4>
            <ul className="record-list">
              {rows.map((citation) => (
                <li key={`${citation.evaluation_id}-${citation.char_start ?? 0}-${citation.quote}`}>
                  <strong>{citation.title}</strong>
                  <span>{citation.employer}</span>
                  <small>{citation.quote}</small>
                  <small>{citation.drift.reason}</small>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </section>
  );
}

function proposalDraft(baseline: string, proposal: MasterCvProposal): string {
  const heading = proposal.kind === "promote_transferable" ? "Promoted claim" : "New claim to add";
  const evidence = proposal.evidence_note ? `\n${proposal.evidence_note}` : "";
  const suffix = `\n\n## ${heading}\n${proposal.requirement_text}${evidence}\n`;
  return baseline + suffix;
}

/** Edit the current master CV without changing committed evaluation snapshots. */
export function MasterCvViewer({
  workspaceId,
  pendingProposal,
  onProposalConsumed,
}: {
  workspaceId: string;
  pendingProposal?: MasterCvProposal | null;
  onProposalConsumed?: () => void;
}) {
  const versions = useMasterCvVersions(workspaceId);
  const [selected, setSelected] = useState("");
  const [compareTo, setCompareTo] = useState("");
  // Freeze the baseline so refetches cannot shift the draft diff.
  const [edit, setEdit] = useState<{ baseline: string; draft: string } | null>(null);
  // Require the save to match the text used by the impact report.
  const [reviewed, setReviewed] = useState<string | null>(null);
  const impact = useMasterCvImpact(workspaceId);
  const save = useSaveMasterCv(workspaceId);
  const rereadCosts = useMasterCvRereadCosts(workspaceId);
  const all = useMemo(() => versions.data ?? [], [versions.data]);
  const currentId = all.find((item) => item.is_current)?.id ?? all[0]?.id ?? "";
  const viewing = selected || currentId;
  const stored = useMasterCv(workspaceId, selected);
  const diff = useMasterCvDiff(workspaceId, compareTo, viewing);
  const draftDiff = useMemo(
    () => (edit && reviewed !== null ? unifiedDiff(edit.baseline, reviewed) : ""),
    [edit, reviewed],
  );

  const [seededProposal, setSeededProposal] = useState<string | null>(null);
  if (
    pendingProposal &&
    seededProposal !== pendingProposal.id &&
    edit === null &&
    stored.data?.content !== undefined
  ) {
    save.reset();
    setEdit({
      baseline: stored.data.content,
      draft: proposalDraft(stored.data.content, pendingProposal),
    });
    setSeededProposal(pendingProposal.id);
    onProposalConsumed?.();
  }

  if (versions.isLoading) return <LoadingState label="Reading stored CV versions" />;
  if (versions.isError) return <ErrorState error={versions.error} />;
  if (!all.length) {
    return (
      <article className="panel">
        <header>
          <div>
            <h2>Stored versions</h2>
            <p>Nothing imported yet.</p>
          </div>
        </header>
        <p className="field-note">Import a CV and its stored text will be readable here.</p>
      </article>
    );
  }

  const stale = edit !== null && reviewed !== null && reviewed !== edit.draft;
  const storedContent = stored.data?.content;
  const closeEditor = () => {
    setEdit(null);
    setReviewed(null);
    impact.reset();
  };
  const saveDraft = (content: string, confirmReread: boolean) =>
    save.mutate(
      { content, confirmReread },
      {
        onSuccess: (result) => {
          if (!result.created) return;
          // Back to whatever is current, or the viewer would sit
          // on the version this save just superseded.
          setSelected("");
          closeEditor();
        },
      },
    );

  return (
    <article className="panel master-cv-viewer">
      <header>
        <div>
          <h2>Stored versions</h2>
          <p>
            {all.length} version{all.length === 1 ? "" : "s"}. Every one stays readable, because a
            citation in a committed evaluation points at the text of its day.
          </p>
        </div>
      </header>
      <div className="panel-body">
        <div className="toolbar">
          <label>
            <span>Version</span>
            <select
              disabled={edit !== null}
              onChange={(event) => setSelected(event.target.value)}
              value={viewing}
            >
              {all.map((item) => (
                <option key={item.id} value={item.id}>
                  {new Date(item.effective_at).toLocaleString()}
                  {item.is_current ? " (current)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Compare with</span>
            <select
              disabled={edit !== null}
              onChange={(event) => setCompareTo(event.target.value)}
              value={compareTo}
            >
              <option value="">No comparison</option>
              {all.map((item) => (
                <option key={item.id} value={item.id}>
                  {new Date(item.effective_at).toLocaleString()}
                </option>
              ))}
            </select>
          </label>
        </div>

        {stored.isError && <ErrorState error={stored.error} />}
        {save.data && (
          <p className="field-note" role="status">
            {save.data.created
              ? `Appended a version, ${save.data.entry_count} citable entries. The version it replaced stays readable in the list above.`
              : "Nothing was appended. The text you saved matches the current version exactly, so there was no new version to record."}
          </p>
        )}

        {edit === null && stored.data && storedContent !== undefined && !compareTo && (
          <>
            {viewing === currentId ? (
              <div className="button-row">
                <button
                  className="button button--primary"
                  onClick={() => {
                    save.reset();
                    setEdit({ baseline: storedContent, draft: storedContent });
                  }}
                  type="button"
                >
                  Edit this CV
                </button>
              </div>
            ) : (
              <p className="field-note">
                Older versions are immutable. Select the current version to edit.
              </p>
            )}
            <MarkdownView className="cv-content" demoteH1>
              {stored.data.content}
            </MarkdownView>
            <details className="cv-integrity">
              <summary>Stored size and checksum</summary>
              <p className="field-note">
                {stored.data.byte_size?.toLocaleString() ?? "?"} bytes, SHA-256{" "}
                <span className="path-value">{stored.data.content_sha256}</span>. Verified on read.
              </p>
            </details>
          </>
        )}

        {edit !== null && (
          <>
            <p className="field-note">
              An evaluation reads its own snapshot of the CV, so editing cannot change what a past
              evaluation said. It can make this CV disagree with one, which the review below names.
            </p>
            <MarkdownWorkspace
              content={edit.draft}
              onChange={(draft) =>
                setEdit((current) => (current ? { ...current, draft } : current))
              }
            />
            <div className="button-row">
              <button
                className="button button--primary"
                disabled={impact.isPending}
                onClick={() =>
                  impact.mutate(edit.draft, { onSuccess: () => setReviewed(edit.draft) })
                }
                type="button"
              >
                {impact.isPending ? "Checking citations..." : "Review changes"}
              </button>
              <ConfirmedActionButton
                className="button button--quiet"
                onConfirm={closeEditor}
                options={{
                  title: "Discard this CV draft?",
                  description:
                    "The current stored CV stays unchanged. Every edit in this draft is lost.",
                  confirmLabel: "Discard draft",
                  danger: true,
                }}
              >
                Discard this draft
              </ConfirmedActionButton>
            </div>
            {impact.isError && <p className="field-error">{impact.error.message}</p>}
            {stale && (
              <p className="field-note">
                The draft moved after the review below was run. Review again before saving.
              </p>
            )}
            {impact.data && !stale && (
              <>
                <UnifiedDiff diff={draftDiff} label="Current version against this draft." />
                <ImpactReport impact={impact.data} />
                {impact.data.identical && (
                  <p className="field-note">
                    This draft is identical to the current version, so saving would append nothing.
                  </p>
                )}
                {impact.data.reread_reason && (
                  <p className="field-note">
                    This draft needs a model re-read because {impact.data.reread_reason}. Fix that
                    in the draft to save it without a charge, or re-read it.
                  </p>
                )}
                <div className="button-row">
                  {impact.data.reread_reason ? (
                    <PaidAction
                      confirmation={{
                        title: "Re-read this CV with the model?",
                        description: `This draft cannot be saved as written, because ${impact.data.reread_reason}. The model re-reads it into the canonical form, and the result is saved as a new version. ${rereadCosts.confirmation(MASTER_CV_REREAD)}`,
                        confirmLabel: "Re-read and save",
                      }}
                      costs={rereadCosts}
                      disabled={save.isPending}
                      label={save.isPending ? "Re-reading" : "Re-read and save"}
                      onAct={() => saveDraft(edit.draft, true)}
                      taskType={MASTER_CV_REREAD}
                    />
                  ) : (
                    <button
                      className="button button--primary"
                      disabled={save.isPending}
                      onClick={() => saveDraft(edit.draft, false)}
                      type="button"
                    >
                      {save.isPending ? "Saving" : "Save as new version"}
                    </button>
                  )}
                </div>
                {save.isError && <p className="field-error">{save.error.message}</p>}
              </>
            )}
          </>
        )}

        {edit === null && compareTo && (
          <>
            {diff.isLoading && <LoadingState label="Comparing versions" />}
            {diff.isError && <ErrorState error={diff.error} />}
            {diff.data && <UnifiedDiff diff={diff.data.diff} label="Selected comparison." />}
          </>
        )}
      </div>
    </article>
  );
}
