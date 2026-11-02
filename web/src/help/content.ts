import { PRODUCT_NAME } from "../productName";

/** One help topic per route or tab that carries a page-header help button. */
export type HelpKey =
  | "home"
  | "cv"
  | "find.boards"
  | "find.review"
  | "find.web"
  | "jobs"
  | "job"
  | "apply.applications"
  | "apply.documents"
  | "settings"
  | "activity";

/** Plain text only: tour strings reach the DOM through innerHTML. */
export interface HelpEntry {
  readonly title: string;
  readonly body: string;
  /** A heading slug in the user guide. */
  readonly docsAnchor?: string;
}

export const USER_GUIDE_URL = "https://github.com/sethstraw/tailorcv/blob/main/docs/user-guide.md";

export const HELP = {
  home: {
    title: "Home",
    body: "Home shows what needs you next. Finish the setup checklist first: a CV, target titles, one board, and a first scan. After that, new postings and open tasks show up here.",
    docsAnchor: "finish-the-setup-walkthrough",
  },
  cv: {
    title: "Your CV",
    body: `Your CV is the only evidence ${PRODUCT_NAME} may cite. Import it once as Markdown, then edit it here. Each save keeps a new version, so older documents still point at the text they used.`,
    docsAnchor: "initialize-one-workspace",
  },
  "find.boards": {
    title: "Boards",
    body: "Boards are the job sites you scan. Add a board, then scan it now or on a schedule. Scans are free and only read public listings. Matching titles go to Review.",
    docsAnchor: "add-boards-and-scan-them-on-a-schedule",
  },
  "find.review": {
    title: "Review",
    body: "Review holds postings that scans found. Keep the ones worth a look and dismiss the rest. A relevance check or full evaluation is paid, and the button shows the estimate first.",
    docsAnchor: "read-the-evidence-not-only-the-score",
  },
  "find.web": {
    title: "Web search",
    body: "Web search asks a model to look past your saved boards. It is paid, and the button shows the estimate before you approve. New boards it finds can join your scan list.",
    docsAnchor: "add-boards-through-guided-search",
  },
  jobs: {
    title: "Jobs",
    body: "Jobs lists every posting you track. Filter by status, open a job to read its evaluation, or compare a few side by side. Start with one posting before you widen the search.",
    docsAnchor: "add-one-posting-before-widening-the-search",
  },
  job: {
    title: "Job",
    body: "This page shows one posting and how your CV fits it. Read the evidence behind each score, not only the number. Tailored documents cite only lines from your CV.",
    docsAnchor: "read-the-evidence-not-only-the-score",
  },
  "apply.applications": {
    title: "Applications",
    body: "Applications tracks where each one stands. Move a card when its status changes. Past stages stay in its history.",
    docsAnchor: "record-applications-without-rewriting-history",
  },
  "apply.documents": {
    title: "Documents",
    body: "Documents holds the CVs and letters you have tailored or imported. An approved document is fixed to its exact bytes. Download it from here or from its job.",
    docsAnchor: "approval-names-exact-bytes",
  },
  settings: {
    title: "Settings",
    body: `Settings holds your search preferences, provider key, and workspace paths. With no provider key ${PRODUCT_NAME} runs in free mode, and paid actions stay off. Add a key to turn them on.`,
    docsAnchor: "choose-the-free-or-paid-path",
  },
  activity: {
    title: "Activity",
    body: "Activity lists background tasks such as scans and evaluations. Each one shows its status and any error. A paid retry asks before it spends.",
  },
} as const satisfies Record<HelpKey, HelpEntry>;

/** One tour step; `element` is a selector the page marks with data-tour. */
export interface TourStep {
  readonly element: string;
  readonly title: string;
  readonly description: string;
}

export const FIND_TOUR = [
  {
    element: '[data-tour="find-tabs"]',
    title: "Three ways to find jobs",
    description:
      "Boards scans sites you choose. Review is where results land. Web search looks wider.",
  },
  {
    element: '[data-tour="find-scan"]',
    title: "Scan your boards",
    description: "A scan is free. It reads public listings and keeps titles that match yours.",
  },
  {
    element: '[data-tour="find-review"]',
    title: "Review what came in",
    description:
      "Keep what looks right and dismiss the rest. Paid checks show their estimate first.",
  },
  {
    element: '[data-tour="page-help"]',
    title: "Help is here",
    description:
      "Open this button on any page for a short guide. You can restart this tour from it.",
  },
] as const satisfies readonly TourStep[];
