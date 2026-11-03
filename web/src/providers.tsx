/* eslint-disable react-refresh/only-export-components -- provider and hook form one public seam */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { PropsWithChildren } from "react";

import { ConfirmationProvider } from "./confirmations";
import { useTaskEventStreams, useTasks } from "./api/hooks";
import { ToastProvider } from "./providers/ToastProvider";

type WorkspaceState = {
  workspaceId: string;
  setWorkspaceId: (value: string) => void;
};

const WorkspaceContext = createContext<WorkspaceState | null>(null);

function WorkspaceTaskEvents({ workspaceId }: { workspaceId: string }) {
  const tasks = useTasks(workspaceId);
  useTaskEventStreams(workspaceId, Array.isArray(tasks.data) ? tasks.data : []);
  return null;
}

export function AppProviders({ children }: PropsWithChildren) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: 15_000, retry: 1, refetchOnWindowFocus: false },
          mutations: { retry: 0 },
        },
      }),
  );
  const [workspaceId, setWorkspaceId] = useState(
    () => window.localStorage.getItem("clearscout.workspace") ?? "",
  );
  useEffect(() => {
    if (workspaceId) window.localStorage.setItem("clearscout.workspace", workspaceId);
  }, [workspaceId]);
  const value = useMemo(() => ({ workspaceId, setWorkspaceId }), [workspaceId]);
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <WorkspaceContext.Provider value={value}>
          <WorkspaceTaskEvents workspaceId={workspaceId} />
          <ConfirmationProvider>{children}</ConfirmationProvider>
        </WorkspaceContext.Provider>
      </ToastProvider>
    </QueryClientProvider>
  );
}

export function useWorkspace() {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("Workspace context is unavailable.");
  return value;
}
