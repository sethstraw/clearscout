import { Fragment, useEffect } from "react";
import { Navigate, useLocation, useSearchParams } from "react-router-dom";

import { Page } from "../components/Page";
import { TabPanel, Tabs } from "../components/Tabs";
import { FindSequence } from "../features/job-search/FindSequence";
import { GuidedCampaignPanel } from "../features/job-search/GuidedCampaignPanel";
import { SearchVocabularyPanel } from "../features/job-search/SearchVocabularyPanel";
import { PendingDiscoveries } from "../features/job-search/PendingDiscoveries";
import { ScanPanel } from "../features/job-search/ScanPanel";
import { ScreenedOutSummary } from "../features/job-search/ScreenedOutSummary";
import { ScreenedOutTitles } from "../features/job-search/ScreenedOutTitles";
import { SearchLoop } from "../features/job-search/SearchLoop";
import { SourceManager } from "../features/job-search/SourceManager";
import { SourceScanHistory } from "../features/job-search/SourceScanHistory";
import { SourceScanOutcomes } from "../features/job-search/SourceScanOutcomes";
import { useWorkspace } from "../providers";

const TAB_ITEMS = [
  { id: "boards", label: "Boards" },
  { id: "review", label: "Review" },
  { id: "web", label: "Web search" },
] as const;

type TabId = (typeof TAB_ITEMS)[number]["id"];
const TAB_IDS: readonly TabId[] = TAB_ITEMS.map((item) => item.id);

/** The Review queue filter a redirected "Screened out" link asks for. */
const SCREENED_OUT_FILTER = { param: "constraint", value: "excluded_only" } as const;

const LEGACY_TABS: Record<string, TabId> = {
  scans: "boards",
  pending: "review",
  refusals: "review",
  campaign: "web",
};

function isTabId(value: string | null): value is TabId {
  return value !== null && (TAB_IDS as readonly string[]).includes(value);
}

/**
 * The address an old tab moved to. A scan's screened-out ledger lives under
 * Boards; a bare "Screened out" link becomes Review with the excluded filter.
 */
function movedTabSearch(params: URLSearchParams): string | null {
  const legacy = params.get("tab");
  const target = legacy ? LEGACY_TABS[legacy] : undefined;
  if (!target) return null;
  const next = new URLSearchParams(params);
  if (legacy === "refusals" && params.has("scan")) {
    next.set("tab", "boards");
  } else {
    next.set("tab", target);
    if (legacy === "refusals") next.set(SCREENED_OUT_FILTER.param, SCREENED_OUT_FILTER.value);
  }
  return `?${next.toString()}`;
}

export function JobSearchPage() {
  const { workspaceId } = useWorkspace();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("tab");
  const scanTaskId = searchParams.get("scan") ?? "";
  const scanSource = searchParams.get("source") ?? "";
  const selected: TabId = isTabId(requested) ? requested : "review";
  const moved = movedTabSearch(searchParams);
  const setTab = (id: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", id);
    setSearchParams(next);
  };
  // React Router does not scroll to a URL hash on its own, so a deep link like
  // ...#screened-candidates needs an explicit scroll once its target mounts.
  useEffect(() => {
    if (moved || selected !== "boards") return undefined;
    const hash = window.location.hash.slice(1);
    if (!hash) return undefined;
    let attempts = 0;
    let timer: number | undefined;
    const tryScroll = () => {
      const target = document.getElementById(hash);
      if (target) {
        target.scrollIntoView({ block: "start" });
        return;
      }
      attempts += 1;
      if (attempts < 40) timer = window.setTimeout(tryScroll, 100);
    };
    tryScroll();
    return () => {
      if (timer) window.clearTimeout(timer);
    };
  }, [moved, selected, scanTaskId]);
  if (moved)
    return (
      <Navigate replace to={{ pathname: location.pathname, search: moved, hash: location.hash }} />
    );
  return (
    <Page
      eyebrow="Find"
      title="Find jobs"
      description="Scan your boards, then review what they found."
      help={`find.${selected}`}
    >
      <div data-tour="find-tabs">
        <Tabs
          id="find-tabs"
          items={TAB_ITEMS}
          label="Find jobs sections"
          onSelect={setTab}
          selected={selected}
        />
      </div>
      <TabPanel id="find-tabs" selected={selected}>
        {selected === "review" && (
          <PendingDiscoveries key={workspaceId} workspaceId={workspaceId} />
        )}
        {selected === "boards" && (
          <>
            <ScanPanel workspaceId={workspaceId} />
            <SourceManager workspaceId={workspaceId} />
            <details className="boards-history" open={Boolean(scanTaskId) || undefined}>
              <summary>History</summary>
              <SourceScanHistory workspaceId={workspaceId} />
              <ScreenedOutSummary workspaceId={workspaceId} />
              <ScreenedOutTitles workspaceId={workspaceId} />
              {scanTaskId && (
                <SourceScanOutcomes
                  key={`${workspaceId}:${scanTaskId}`}
                  source={scanSource}
                  onSourceChange={(source) =>
                    setSearchParams((current) => {
                      const next = new URLSearchParams(current);
                      if (source) next.set("source", source);
                      else next.delete("source");
                      return next;
                    })
                  }
                  taskId={scanTaskId}
                  workspaceId={workspaceId}
                />
              )}
            </details>
          </>
        )}
        {selected === "web" && (
          <Fragment key={workspaceId}>
            <GuidedCampaignPanel workspaceId={workspaceId} />
            <SearchVocabularyPanel workspaceId={workspaceId} />
          </Fragment>
        )}
      </TabPanel>
      <details className="find-help">
        <summary>How finding jobs works</summary>
        <FindSequence />
        <SearchLoop />
      </details>
    </Page>
  );
}
