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
    body: "Home shows what needs you next. Finish the setup checklist first: a CV, target titles, one board, and a first scan. Best fit uses your fit review when you entered one, and otherwise the CV evidence and role fit. Every move of an application adds to its history, so nothing is overwritten. Background tasks survive a restart, and Activity lists them all.",
    docsAnchor: "finish-the-setup-walkthrough",
  },
  cv: {
    title: "Your CV",
    body: `Your CV is the only evidence ${PRODUCT_NAME} may cite. Import it once, then edit it here. A Markdown or text file in the CV format is imported as is. Any other file is re-read by the model, which is paid, and you confirm the estimate first. Review the result before you use it. Each save keeps a new version, so older evaluations still point at the text they used.`,
    docsAnchor: "initialize-one-workspace",
  },
  "find.boards": {
    title: "Boards",
    body: `Boards are the job sites you scan. Save a target title in Settings > Search, add a board, then scan it now or on a schedule. Scans are free and only read public listings. Titles that match yours go to Review. A recognized board does not promise permission or full coverage, so check each board's last read. A scheduled scan runs only while ${PRODUCT_NAME} is running. Past scans, screened-out titles, and each scan's ledger are under History.`,
    docsAnchor: "add-boards-and-scan-them-on-a-schedule",
  },
  "find.review": {
    title: "Review",
    body: "Review holds postings that scans and web search found. Track a job to save it, or dismiss it. Every dismissal can be undone from Dismissed discoveries. Full evaluation compares a posting with your CV. It and the relevance review are paid, and each button shows its estimate first. Content readiness shows which postings can be evaluated now and which need a refresh first. Main queue keeps postings with unconfirmed facts, such as location or pay, unless you turn on Separate uncertain postings. The Screened out filter lists postings that conflict with your search rules. Filters change what you see, not your rules. Favorite marks your best matches. Keep marks rows the program misjudged. Both survive bulk and program dismissals. Refresh list reads from the top, so the order can change. Closed posting history lists postings that left the queue after a closure. A later active read brings one back. Keys: j and k move, e evaluates, r reviews, x dismisses, u undoes.",
    docsAnchor: "read-the-evidence-not-only-the-score",
  },
  "find.web": {
    title: "Web search",
    body: `Scan your saved boards first. Web search then asks a model to look past them for new postings and employer boards. It is paid, and the button shows the estimate before you approve. Every planned attempt runs once you confirm, and provider content charges can exceed the estimate. While it runs, other paid tasks wait, and you cannot stop it from here. New postings land in Review, and new boards can join your scan list. Propose new search terms is one paid call that reads your CV and finds no jobs. Its suggestions widen later scans and web searches only after you approve them. After a search, keep or dismiss what it found in Review, then open a job to tailor your application. Spending totals are in Settings, under Costs.`,
    docsAnchor: "add-boards-through-guided-search",
  },
  jobs: {
    title: "Jobs",
    body: "Jobs lists every posting you track. Filter by status, open a job to read its evaluation, or compare a few side by side. Scores stay separate, so no single number decides for you. Refresh re-reads postings from their publishers. Saved views, archived jobs, and the evidence filter sit under Advanced. Start with one posting before you widen the search.",
    docsAnchor: "add-one-posting-before-widening-the-search",
  },
  job: {
    title: "Job",
    body: "This page shows one posting and how your CV fits it. Read the evidence behind each score, not only the number. Evaluate from the page header: each run stores a new evaluation version against the current posting, prompts, settings, and CV. Dispute re-checks sit under Fit, in Advanced. Studio work runs in the background, so closing the page keeps its progress. Tailored documents cite only lines from your CV, and each line needs your decision before approval.",
    docsAnchor: "read-the-evidence-not-only-the-score",
  },
  "apply.applications": {
    title: "Applications",
    body: "Applications tracks where each one stands. Move a card when its status changes. Prepared is not submitted, so record the submission when you send it. Every status change stays visible in its history.",
    docsAnchor: "record-applications-without-rewriting-history",
  },
  "apply.documents": {
    title: "Documents",
    body: "Documents holds the CVs and letters you have tailored or imported. Every version is kept and hashed, and an approved document is fixed to its exact bytes. Download it from here or from its job. Imported files with no job sit in a closed section at the bottom.",
    docsAnchor: "approval-names-exact-bytes",
  },
  settings: {
    title: "Settings",
    body: `Settings holds your search preferences, provider key, and workspace paths. With no provider key ${PRODUCT_NAME} runs in free mode, and paid actions stay off. Add a key to turn them on.`,
    docsAnchor: "choose-the-free-or-paid-path",
  },
  activity: {
    title: "Activity",
    body: "Activity lists background tasks such as scans and evaluations, including work that survived a restart. Each one shows its status and any error. A paid retry shows its recorded cost first and asks before it runs.",
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
