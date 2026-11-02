import { Fragment, useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { Page } from "../components/Page";
import { TabPanel, Tabs } from "../components/Tabs";
import { FindSequence } from "../features/job-search/FindSequence";
import { GuidedCampaignPanel } from "../features/job-search/GuidedCampaignPanel";
import { SearchVocabularyPanel } from "../features/job-search/SearchVocabularyPanel";
import { PendingDiscoveries } from "../features/job-search/PendingDiscoveries";
import { PostingRefusals } from "../features/job-search/PostingRefusals";
import { RefusalPatternProposals } from "../features/job-search/RefusalPatternProposals";
import { ScanPanel } from "../features/job-search/ScanPanel";
import { ScreenedOutSummary } from "../features/job-search/ScreenedOutSummary";
import { ScreenedOutTitles } from "../features/job-search/ScreenedOutTitles";
import { SearchLoop } from "../features/job-search/SearchLoop";
import { SourceScanHistory } from "../features/job-search/SourceScanHistory";
import { SourceScanOutcomes } from "../features/job-search/SourceScanOutcomes";
import { useWorkspace } from "../providers";

const TAB_ITEMS = [
  { id: "scans", label: "Scan for openings" },
  { id: "campaign", label: "Find new jobs" },
  { id: "pending", label: "Review found jobs" },
  { id: "refusals", label: "Screened out" },
] as const;

type TabId = (typeof TAB_ITEMS)[number]["id"];
const TAB_IDS: readonly TabId[] = TAB_ITEMS.map((item) => item.id);

function isTabId(value: string | null): value is TabId {
  return value !== null && (TAB_IDS as readonly string[]).includes(value);
}

export function JobSearchPage() {
  const { workspaceId } = useWorkspace();
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("tab");
  const scanTaskId = searchParams.get("scan") ?? "";
  const scanSource = searchParams.get("source") ?? "";
  const selected: TabId = isTabId(requested) ? requested : "pending";
  const setTab = (id: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", id);
    setSearchParams(next);
  };
  // React Router does not scroll to a URL hash on its own, so a deep link like
  // ...#screened-candidates needs an explicit scroll once its target mounts.
  useEffect(() => {
    if (selected !== "refusals") return undefined;
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
  }, [selected, scanTaskId]);
  return (
    <Page
      eyebrow="Find"
      title="Find jobs"
      description="Find openings, then review the jobs worth pursuing."
    >
      <Tabs
        id="find-tabs"
        items={TAB_ITEMS}
        label="Find jobs sections"
        onSelect={setTab}
        selected={selected}
      />
      <TabPanel id="find-tabs" selected={selected}>
        {selected === "pending" && (
          <PendingDiscoveries key={workspaceId} workspaceId={workspaceId} />
        )}
        {selected === "scans" && (
          <>
            <ScanPanel workspaceId={workspaceId} />
            <SourceScanHistory workspaceId={workspaceId} />
          </>
        )}
        {selected === "campaign" && (
          <Fragment key={workspaceId}>
            <GuidedCampaignPanel workspaceId={workspaceId} />
            <SearchVocabularyPanel workspaceId={workspaceId} />
          </Fragment>
        )}
        {selected === "refusals" && (
          <>
            <RefusalPatternProposals workspaceId={workspaceId} />
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
            <PostingRefusals workspaceId={workspaceId} />
          </>
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
