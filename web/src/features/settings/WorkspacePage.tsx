import { useState } from "react";

import { Page } from "../../components/Page";
import { Tabs } from "../../components/Tabs";
import { WorkspaceRecords } from "../workspace-records/WorkspaceRecords";
import { useWorkspace } from "../../providers";

const tabs = [
  { id: "notes", label: "Notes" },
  { id: "metadata", label: "Tags & saved views" },
] as const;
type WorkspaceTab = (typeof tabs)[number]["id"];

export function WorkspacePage() {
  const { workspaceId } = useWorkspace();
  const [tab, setTab] = useState<WorkspaceTab>("notes");
  return (
    <Page
      eyebrow="Records"
      title="Notes, tags, saved views"
      description="How this workspace is organised. User-entered throughout."
    >
      <Tabs
        id="workspace-tabs"
        items={tabs}
        label="Workspace record sections"
        onSelect={(id) => setTab(id as WorkspaceTab)}
        selected={tab}
      />
      <WorkspaceRecords selected={tab} workspaceId={workspaceId} />
    </Page>
  );
}
