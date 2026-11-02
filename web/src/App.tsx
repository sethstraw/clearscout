import { lazy, Suspense, useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";

import { useHealth, useTasks, useWorkspaces } from "./api/hooks";
import { BrandMark } from "./components/Brand";
import { Icons } from "./components/Icons";
import { ErrorState, LoadingState } from "./components/States";
import { JobSearchPage } from "./pages/JobSearchPage";
import { useWorkspace } from "./providers";
import { WelcomePage } from "./pages/WelcomePage";

const ApplicationsPage = lazy(() =>
  import("./pages/ApplicationsPage").then((module) => ({ default: module.ApplicationsPage })),
);
const ApplicationFilesPage = lazy(() =>
  import("./pages/ApplicationFilesPage").then((module) => ({
    default: module.ApplicationFilesPage,
  })),
);
const ApiPlaygroundPage = lazy(() =>
  import("./pages/ApiPlaygroundPage").then((module) => ({ default: module.ApiPlaygroundPage })),
);
const CommandCenterPage = lazy(() =>
  import("./pages/CommandCenterPage").then((module) => ({ default: module.CommandCenterPage })),
);
const JobPage = lazy(() =>
  import("./pages/JobPage").then((module) => ({ default: module.JobPage })),
);
const JobsPage = lazy(() =>
  import("./pages/JobsPage").then((module) => ({ default: module.JobsPage })),
);
const MasterCvPage = lazy(() =>
  import("./pages/MasterCvPage").then((module) => ({ default: module.MasterCvPage })),
);
const NotFoundPage = lazy(() =>
  import("./pages/NotFoundPage").then((module) => ({ default: module.NotFoundPage })),
);
const ScoringPage = lazy(() =>
  import("./pages/ScoringPage").then((module) => ({ default: module.ScoringPage })),
);
const SettingsPage = lazy(() =>
  import("./pages/SettingsPage").then((module) => ({ default: module.SettingsPage })),
);
const SpendPage = lazy(() =>
  import("./pages/SpendPage").then((module) => ({ default: module.SpendPage })),
);
const PromptsPage = lazy(() =>
  import("./pages/PromptsPage").then((module) => ({ default: module.PromptsPage })),
);
const TasksPage = lazy(() =>
  import("./pages/TasksPage").then((module) => ({ default: module.TasksPage })),
);
const DocumentsPage = lazy(() =>
  import("./pages/DocumentsPage").then((module) => ({ default: module.DocumentsPage })),
);
const WorkspacePage = lazy(() =>
  import("./pages/WorkspacePage").then((module) => ({ default: module.WorkspacePage })),
);

// A page chunk can still fail to arrive. Offer a recovery action once it runs long.
export function RouteFallback() {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setSlow(true), 12000);
    return () => window.clearTimeout(timer);
  }, []);
  return (
    <div className="route-fallback">
      <LoadingState label="Opening page" />
      {slow ? (
        <div className="route-fallback__recover" role="status">
          <p>This page's code is taking longer than usual to load.</p>
          <button
            className="button button--primary"
            onClick={() => window.location.reload()}
            type="button"
          >
            Reload this page
          </button>
        </div>
      ) : null}
    </div>
  );
}

/**
 * Group navigation by job-search stage. The short label rides under the icon in the
 * narrow bottom bar, where the full label does not fit; aria-label keeps the full
 * label as the accessible name in every layout.
 */
const navigation = [
  {
    section: "Today",
    items: [{ to: "/", label: "Command Center", short: "Command", icon: Icons.home }],
  },
  {
    section: "Your CV",
    items: [{ to: "/cv", label: "Master CV", short: "CV", icon: Icons.cv }],
  },
  {
    section: "Find",
    items: [{ to: "/find", label: "Find jobs", short: "Find", icon: Icons.search }],
  },
  {
    section: "Decide",
    items: [{ to: "/jobs", label: "Tracked postings", short: "Postings", icon: Icons.jobs }],
  },
  {
    section: "Apply",
    items: [
      {
        to: "/applications",
        label: "Applications",
        short: "Applications",
        icon: Icons.applications,
      },
      {
        to: "/documents",
        label: "Tailored documents",
        short: "Tailored",
        icon: Icons.documents,
      },
    ],
  },
  {
    section: "Records",
    items: [
      { to: "/records", label: "Notes, tags, saved views", short: "Notes", icon: Icons.notes },
    ],
  },
  {
    section: "System",
    items: [{ to: "/tasks", label: "Background tasks", short: "Tasks", icon: Icons.tasks }],
  },
  {
    section: "Settings",
    items: [
      { to: "/prompts", label: "Prompts", short: "Prompts", icon: Icons.prompts },
      { to: "/settings", label: "Settings", short: "Settings", icon: Icons.settings },
    ],
  },
];

function prefersCollapsedNav() {
  return window.localStorage.getItem("tailorcv.nav") === "collapsed";
}

function prefersDarkTheme() {
  const stored = window.localStorage.getItem("tailorcv.theme");
  return stored ? stored === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function App() {
  const health = useHealth();
  const workspaces = useWorkspaces();
  const { workspaceId, setWorkspaceId } = useWorkspace();
  const tasks = useTasks(workspaceId);
  const [dark, setDark] = useState(prefersDarkTheme);
  // Collapse navigation on narrow screens to preserve ledger width.
  const [navCollapsed, setNavCollapsed] = useState(prefersCollapsedNav);

  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    window.localStorage.setItem("tailorcv.theme", dark ? "dark" : "light");
  }, [dark]);

  useEffect(() => {
    window.localStorage.setItem("tailorcv.nav", navCollapsed ? "collapsed" : "expanded");
  }, [navCollapsed]);

  useEffect(() => {
    const available = workspaces.data ?? [];
    if (available.length > 0 && !available.some((item) => item.id === workspaceId)) {
      setWorkspaceId(available[0]?.id ?? "");
    }
  }, [setWorkspaceId, workspaceId, workspaces.data]);

  const activeTasks = (tasks.data ?? []).filter((task) =>
    ["queued", "running", "needs_review", "committing"].includes(task.status),
  );

  if (workspaces.isLoading) return <LoadingState label="Opening your workspace" />;
  if (workspaces.isError) return <ErrorState error={workspaces.error} />;
  if (!workspaces.data?.length) {
    return (
      <Routes>
        <Route path="/welcome" element={<WelcomePage />} />
        <Route path="*" element={<Navigate replace to="/welcome" />} />
      </Routes>
    );
  }

  return (
    <div className="app-shell" data-nav={navCollapsed ? "collapsed" : "expanded"}>
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <aside className="sidebar" aria-label="Primary navigation">
        <div className="brand">
          <BrandMark />
          <div>
            <strong>TailorCV</strong>
            <span>Follow the evidence path.</span>
          </div>
        </div>
        <button
          aria-label={navCollapsed ? "Expand navigation" : "Collapse navigation"}
          className="nav-toggle"
          onClick={() => setNavCollapsed((value) => !value)}
          title={navCollapsed ? "Expand navigation" : "Collapse navigation"}
          type="button"
        >
          {navCollapsed ? (
            <Icons.chevronRight height={14} width={14} />
          ) : (
            <>
              <Icons.chevronLeft height={14} width={14} />
              Collapse
            </>
          )}
        </button>
        <nav>
          {navigation.map(({ section, items }) => (
            <div className="nav-section" key={section}>
              <p className="nav-section-label">{section}</p>
              {items.map(({ to, label, short, icon: Icon }) => {
                const badge = to === "/tasks" && activeTasks.length > 0 ? activeTasks.length : 0;
                const ariaLabel = badge > 0 ? `${label}, ${badge} active` : label;
                return (
                  <NavLink
                    aria-label={ariaLabel}
                    className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
                    end={to === "/"}
                    key={to}
                    to={to}
                  >
                    <Icon />
                    <span className="nav-link-label">{label}</span>
                    <span className="nav-link-short">{short}</span>
                    {badge > 0 ? (
                      <span aria-hidden="true" className="nav-link-badge">
                        {badge}
                      </span>
                    ) : null}
                  </NavLink>
                );
              })}
            </div>
          ))}
        </nav>
        <div className="sidebar-footer">
          <label htmlFor="workspace-select">Workspace</label>
          <select
            id="workspace-select"
            onChange={(event) => setWorkspaceId(event.target.value)}
            value={workspaceId}
          >
            {workspaces.data.map((workspace) => (
              <option key={workspace.id} value={workspace.id}>
                {workspace.name}
              </option>
            ))}
          </select>
          <button
            aria-label={`Use ${dark ? "light" : "dark"} theme`}
            className="icon-button"
            onClick={() => setDark((value) => !value)}
            type="button"
          >
            {dark ? <Icons.sun /> : <Icons.moon />}
          </button>
          {/* A server-rendered page outside the router, so not a NavLink. */}
          <a className="sidebar-tool-link" href="/admin/">
            Data browser
          </a>
        </div>
      </aside>
      <div className="content">
        {health.data && !health.data.worker_available && activeTasks.length > 0 ? (
          <aside
            aria-label="Background worker is not running"
            className="worker-availability"
            role="status"
          >
            <strong>Background worker is not running.</strong>
            <span>
              Start <code>tailorcv worker</code> in another terminal before queueing background
              work.
            </span>
          </aside>
        ) : null}
        <Suspense fallback={<RouteFallback />}>
          <Routes>
            <Route path="/" element={<CommandCenterPage />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/jobs/:jobId" element={<JobPage />} />
            <Route path="/applications" element={<ApplicationsPage />} />
            <Route path="/applications/:applicationId" element={<ApplicationFilesPage />} />
            <Route path="/documents" element={<DocumentsPage />} />
            <Route path="/records" element={<WorkspacePage />} />
            <Route path="/find" element={<JobSearchPage />} />
            <Route path="/find/scoring" element={<ScoringPage />} />
            <Route path="/cv" element={<MasterCvPage />} />
            <Route path="/prompts" element={<PromptsPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/api-playground" element={<ApiPlaygroundPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/spend" element={<SpendPage />} />
            <Route path="/welcome" element={<WelcomePage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </Suspense>
      </div>
    </div>
  );
}
