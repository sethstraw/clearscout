import { Page } from "../../components/Page";
import { SpendReport } from "../spend/SpendReport";
import { useWorkspace } from "../../providers";

export function SpendPage() {
  const { workspaceId } = useWorkspace();
  return (
    <Page
      eyebrow="Decide"
      title="Model spend"
      description="Recorded provider costs, per stage, per model, and per call, with the totals a person can audit against the account."
    >
      <SpendReport workspaceId={workspaceId} />
    </Page>
  );
}
