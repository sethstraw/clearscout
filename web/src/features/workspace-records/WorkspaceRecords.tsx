import { TabPanel } from "../../components/Tabs";
import { MetadataWorkspace } from "./MetadataWorkspace";
import { NotesWorkspace } from "./NotesWorkspace";

type WorkspaceRecordTab = "notes" | "metadata";

export function WorkspaceRecords({
  selected,
  workspaceId,
}: {
  selected: WorkspaceRecordTab;
  workspaceId: string;
}) {
  return (
    <TabPanel id="workspace-tabs" selected={selected}>
      {selected === "notes" && <NotesWorkspace workspaceId={workspaceId} />}
      {selected === "metadata" && <MetadataWorkspace workspaceId={workspaceId} />}
    </TabPanel>
  );
}
