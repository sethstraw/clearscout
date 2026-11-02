import { useSearchParams } from "react-router-dom";

import { Page } from "../components/Page";
import { TabPanel, Tabs } from "../components/Tabs";
import { ApplicationsPage } from "./ApplicationsPage";
import { DocumentsPage } from "./DocumentsPage";

const APPLY_TABS = [
  { id: "applications", label: "Applications" },
  { id: "documents", label: "Documents" },
] as const;
type ApplyTab = (typeof APPLY_TABS)[number]["id"];

function isApplyTab(value: string | null): value is ApplyTab {
  return APPLY_TABS.some((item) => item.id === value);
}

/** Applications and their tailored documents, one tab each. */
export function ApplyPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("tab");
  const tab: ApplyTab = isApplyTab(requested) ? requested : "applications";
  const setTab = (id: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", id);
    setSearchParams(next, { replace: true });
  };
  return (
    <Page
      eyebrow="Apply"
      title="Apply"
      description="Track each application and the documents made for it."
      help={`apply.${tab}`}
    >
      <Tabs
        id="apply-tabs"
        items={APPLY_TABS}
        label="Apply sections"
        onSelect={setTab}
        selected={tab}
      />
      <TabPanel id="apply-tabs" selected={tab}>
        {tab === "applications" && <ApplicationsPage />}
        {tab === "documents" && <DocumentsPage />}
      </TabPanel>
    </Page>
  );
}
