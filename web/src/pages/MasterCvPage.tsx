import { useState } from "react";

import type { MasterCvProposal } from "../api/client";
import {
  MASTER_CV_REREAD,
  useImportMasterCv,
  useMasterCvRereadCosts,
  useMasterCvVersions,
} from "../api/hooks";
import { ApiError } from "../api/http";
import { ActionWithReason } from "../components/ActionWithReason";
import { Page } from "../components/Page";
import { PaidAction } from "../components/PaidAction";
import { Toggletip } from "../components/Toggletip";
import { needsModelRead } from "../features/master-cv/importCost";
import { MasterCvProposals } from "../features/master-cv/MasterCvProposals";
import { MasterCvViewer } from "../features/master-cv/MasterCvViewer";
import { RequirementGaps } from "../features/master-cv/RequirementGaps";
import { useWorkspace } from "../providers";

export function MasterCvPage() {
  const { workspaceId } = useWorkspace();
  const versions = useMasterCvVersions(workspaceId);
  const importCv = useImportMasterCv(workspaceId);
  const rereadCosts = useMasterCvRereadCosts(workspaceId);
  const [masterCv, setMasterCv] = useState<File>();
  const [pendingProposal, setPendingProposal] = useState<MasterCvProposal | null>(null);
  const noCv = versions.data !== undefined && versions.data.length === 0;
  // The server refuses a text file that needs the model until the re-read is confirmed.
  const rereadRefused =
    importCv.error instanceof ApiError && importCv.error.status === 409 && Boolean(masterCv);
  const paidImport = Boolean(masterCv && (needsModelRead(masterCv.name) || rereadRefused));
  const confirmation = {
    title: "Re-read this CV with the model?",
    description: `The model re-reads ${masterCv?.name ?? "this file"} into the canonical form. Review the result before you use it. ${rereadCosts.confirmation(MASTER_CV_REREAD)}`,
    confirmLabel: "Re-read and import",
  };
  return (
    <Page
      eyebrow="Evidence"
      title="CV"
      description="The evidence every evaluation cites."
      help="cv"
    >
      <section className="cv-layout">
        <details className="cv-import" open={noCv || undefined}>
          <summary>Import a version from a file</summary>
          <form
            className="cv-import-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (masterCv) importCv.mutate({ file: masterCv, confirmReread: false });
            }}
          >
            <label className="file-field">
              <span>CV source</span>
              <input
                accept=".docx,.pdf,.md,.markdown,.txt,.png,.jpg,.jpeg,.gif,.webp"
                onChange={(event) => {
                  importCv.reset();
                  setMasterCv(event.target.files?.[0]);
                }}
                type="file"
              />
            </label>
            <Toggletip label="Which files can I import?">
              Word, PDF, Markdown, plain text, or a photo of a printed CV. The import becomes your
              current CV, and older versions stay attached to their evaluations.
            </Toggletip>
            {paidImport ? (
              <PaidAction
                confirmation={confirmation}
                costs={rereadCosts}
                disabled={importCv.isPending}
                label={importCv.isPending ? "Re-reading and importing" : "Import as new version"}
                onAct={() => {
                  if (masterCv) importCv.mutate({ file: masterCv, confirmReread: true });
                }}
                taskType={MASTER_CV_REREAD}
              />
            ) : (
              <ActionWithReason
                disabledReason={masterCv ? undefined : "Choose a CV file first"}
                pending={importCv.isPending}
                type="submit"
              >
                {importCv.isPending ? "Checking and importing" : "Import as new version"}
              </ActionWithReason>
            )}
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
        <details className="cv-advanced">
          <summary>Advanced</summary>
          <MasterCvProposals
            workspaceId={workspaceId}
            onAccepted={(proposal) => setPendingProposal(proposal)}
          />
          <RequirementGaps workspaceId={workspaceId} />
        </details>
      </section>
    </Page>
  );
}
