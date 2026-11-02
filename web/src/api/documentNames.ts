/** Human-readable names for stored files. */
import type { components } from "./schema";

/** The generated artifact vocabulary that document names are keyed by. */
export type ArtifactLogicalType = components["schemas"]["ArtifactResponse"]["logical_type"];

const DOCUMENT_NAMES: Record<ArtifactLogicalType, string> = {
  "resume.markdown": "Tailored CV",
  "cover_letter.markdown": "Cover letter",
  "document.rendered.pdf": "Rendered PDF",
  "document.rendered.text": "Rendered text",
  "evaluation.fit_report.markdown": "Fit report",
  "screening.report.markdown": "Screening report",
  "batch.report.markdown": "Comparison report",
  "tailoring.report.markdown": "Tailoring report",
  "tailoring.review.markdown": "Review notes",
  "master_cv.snapshot.markdown": "Master CV as used",
  "master_cv.snapshot.text": "Master CV as used",
  "posting.body.text": "Posting as captured",
  "posting.body.html": "Posting markup as published",
  "posting.listing.text": "Board listing as captured",
  "posting.source.json": "Posting record as published",
  "posting.source.html": "Posting page as fetched",
  "posting.source.xml": "Posting feed item as read",
  "posting.source.pdf": "Posting source file",
  "posting.source.image": "Posting source file",
  "tailoring.manifest.json": "Tailoring run record",
  "tailoring.review.json": "Review record",
  "batch.record.json": "Comparison record",
  "discovery.session.json": "Gap exploration record",
  "company_research.posting.text": "Posting used as research",
  "company_research.careers.text": "Careers page as read",
  "company_research.about.text": "About page as read",
  "user.attachment": "File you added",
  "recruiter.email": "Recruiter email",
  take_home: "Take-home exercise",
};

/** The vocabulary as values, for the pickers that offer a label. */
export const ARTIFACT_LOGICAL_TYPES = Object.keys(DOCUMENT_NAMES) as ArtifactLogicalType[];

/** Where a file came from, for a list that gathers several runs together. */
const FILED_BY: Record<string, string> = {
  job_posting: "This job",
  application: "This application",
  job_evaluation: "Evaluation",
  tailoring_run: "Tailoring",
  posting_observation: "Posting check",
  master_cv_version: "Master CV",
};

export function documentName(logicalType: ArtifactLogicalType): string {
  return DOCUMENT_NAMES[logicalType];
}

/** The path an import recorded, which is empty for a file this application wrote. */
export function storedPath(provenance: Record<string, unknown>): string {
  const value = provenance.relative_path;
  return typeof value === "string" ? value : "";
}

// An import files each tailoring run in its own directory under `runs/`, and the
// files inside those directories carry one name across every run.
function directoryUnderRuns(parts: string[]): string {
  const runsAt = parts.lastIndexOf("runs");
  return runsAt >= 0 && runsAt + 2 < parts.length ? (parts[runsAt + 1] ?? "") : "";
}

/** The name an import kept, qualified by the directory under `runs/` when the file sits below one. */
export function storedFileName(provenance: Record<string, unknown>): string {
  const path = storedPath(provenance);
  if (!path) return "";
  const parts = path.split("/");
  const name = parts[parts.length - 1] ?? path;
  const directory = directoryUnderRuns(parts);
  return directory ? `${directory}/${name}` : name;
}

export function filedBy(relatedEntityType: string | null | undefined): string {
  if (!relatedEntityType) return "Imported";
  return FILED_BY[relatedEntityType] ?? relatedEntityType.replaceAll("_", " ");
}
