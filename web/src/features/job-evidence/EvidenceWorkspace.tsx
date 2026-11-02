import type { JobDetail } from "../../api/client";
import { EvidenceMap, UnsupportedEvidence, type EvidenceSourceQuery } from "./EvidencePanels";
import {
  groupRequirements,
  isAnswered,
  unattachedEvidence,
  type FitRequirement,
} from "./evidenceWorkspaceModel";

export function EvidenceWorkspace({
  data,
  evidenceSource,
  fitReportPending = false,
  fitRequirements,
  jobId,
  onSelectEvidence,
  onToggleUnsupported,
  selectedEvidenceId,
  unsupportedOnly,
}: {
  data: JobDetail;
  evidenceSource: EvidenceSourceQuery;
  fitReportPending?: boolean;
  fitRequirements: readonly FitRequirement[] | undefined;
  jobId: string;
  onSelectEvidence: (evidenceId: string) => void;
  onToggleUnsupported: () => void;
  selectedEvidenceId: string;
  unsupportedOnly: boolean;
}) {
  const requirements = groupRequirements(
    data.evidence,
    data.requirements ?? [],
    fitRequirements,
  ).map((requirement) => ({
    ...requirement,
    answered: isAnswered(requirement),
  }));
  // Only the fit report pairs gate quotes with cards, so nothing is unattached until it settles.
  const unattached = fitReportPending
    ? []
    : unattachedEvidence(data.evidence, data.requirements ?? [], fitRequirements);
  const answered = requirements.filter((requirement) => requirement.answered).length;

  return (
    <section className="evidence-workspace">
      <UnsupportedEvidence
        items={data.unsupported}
        onToggle={onToggleUnsupported}
        unsupportedOnly={unsupportedOnly}
      />
      {!unsupportedOnly && (
        <EvidenceMap
          answered={answered}
          evidenceSource={evidenceSource}
          factors={data.factors}
          fitReportPending={fitReportPending}
          jobId={jobId}
          onSelectEvidence={onSelectEvidence}
          requirements={requirements}
          selectedEvidenceId={selectedEvidenceId}
          unattached={unattached}
        />
      )}
    </section>
  );
}
