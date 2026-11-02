import { useState } from "react";

import type { MasterCvProposal } from "../api/client";
import { useImportMasterCv } from "../api/hooks";
import { ActionWithReason } from "../components/ActionWithReason";
import { importCostNote } from "../features/master-cv/importCost";
import { Page } from "../components/Page";
import { MasterCvProposals } from "../features/master-cv/MasterCvProposals";
import { MasterCvViewer } from "../features/master-cv/MasterCvViewer";
import { RequirementGaps } from "../features/master-cv/RequirementGaps";
import { useWorkspace } from "../providers";

export function MasterCvPage() {
  const { workspaceId } = useWorkspace();
  const importCv = useImportMasterCv(workspaceId);
  const [masterCv, setMasterCv] = useState<File>();
  const [pendingProposal, setPendingProposal] = useState<MasterCvProposal | null>(null);
  return (
    <Page
      eyebrow="Your CV"
      title="Master CV"
      description="The evidence every evaluation cites. Imports may use a paid provider to restructure your file; review the result before using it."
    >
      <section className="cv-layout">
        <details className="cv-import">
          <summary>Import a version from a file</summary>
          <form
            className="cv-import-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (masterCv) importCv.mutate(masterCv);
            }}
          >
            <label className="file-field">
              <span>CV source</span>
              <input
                accept=".docx,.pdf,.md,.markdown,.txt,.png,.jpg,.jpeg,.gif,.webp"
                onChange={(event) => setMasterCv(event.target.files?.[0])}
                required
                type="file"
              />
            </label>
            <ActionWithReason
              disabledReason={masterCv ? undefined : "Choose a CV file first"}
              pending={importCv.isPending}
              type="submit"
            >
              {importCv.isPending ? "Checking and importing" : "Import as new version"}
            </ActionWithReason>
            <p className="field-note">
              Word, PDF, Markdown, plain text, or a photograph of a printed CV. A replacement
              becomes the canonical CV, while the prior immutable version stays attached to its
              earlier evaluations.
            </p>
            {masterCv && <p className="field-note">{importCostNote(masterCv.name)}</p>}
            {importCv.isError && <p className="field-error">{importCv.error.message}</p>}
            {importCv.data && (
              <p role="status">
                Ready: {importCv.data.entry_count} citable entries ({importCv.data.method}).
              </p>
            )}
          </form>
        </details>
        <MasterCvViewer
          workspaceId={workspaceId}
          pendingProposal={pendingProposal}
          onProposalConsumed={() => setPendingProposal(null)}
        />
        <MasterCvProposals
          workspaceId={workspaceId}
          onAccepted={(proposal) => setPendingProposal(proposal)}
        />
        <RequirementGaps workspaceId={workspaceId} />
      </section>
    </Page>
  );
}
