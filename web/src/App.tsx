import { lazy, Suspense, useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";

import { useHealth, useTasks, useWorkspaces } from "./api/hooks";
import { BrandMark } from "./components/Brand";
import { Icons } from "./components/Icons";
import { ErrorState, LoadingState } from "./components/States";
import { JobSearchPage } from "./pages/JobSearchPage";
import { PRODUCT_NAME } from "./productName";
import { useWorkspace } from "./providers";
import { WelcomePage } from "./pages/WelcomePage";

const ApplicationFilesPage = lazy(() =>
  import("./pages/ApplicationFilesPage").then((module) => ({
    default: module.ApplicationFilesPage,
  })),
);
const ApplyPage = lazy(() =>
  import("./pages/ApplyPage").then((module) => ({ default: module.ApplyPage })),
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
const TasksPage = lazy(() =>
  import("./pages/TasksPage").then((module) => ({ default: module.TasksPage })),
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
 * The short label rides under the icon in the narrow bar, where the full label
 * does not fit; aria-label keeps the full label as the accessible name.
 */
const navigation = [
  { to: "/", label: "Home", short: "Home", icon: Icons.home },
  { to: "/cv", label: "CV", short: "CV", icon: Icons.cv },
  { to: "/find", label: "Find jobs", short: "Find", icon: Icons.search },
  { to: "/jobs", label: "Jobs", short: "Jobs", icon: Icons.jobs },
  { to: "/apply", label: "Apply", short: "Apply", icon: Icons.applications },
  { to: "/settings", label: "Settings", short: "Settings", icon: Icons.settings },
] as const;

/** Old addresses that still arrive from bookmarks, docs, and server redirects. */
const MOVED_ROUTES = [
  ["/applications", "/apply?tab=applications"],
  ["/documents", "/apply?tab=documents"],
  ["/records", "/settings?tab=records"],
  ["/prompts", "/settings?tab=advanced&section=prompts"],
  ["/spend", "/settings?tab=costs"],
  ["/api-playground", "/settings?tab=advanced&section=diagnostics"],
] as const;

function navLinkClass({ isActive }: { isActive: boolean }) {
  return isActive ? "nav-link active" : "nav-link";
}

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

  const badge = activeTasks.length;
  return (
    <div className="app-shell" data-nav={navCollapsed ? "collapsed" : "expanded"}>
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <aside className="sidebar" aria-label="Primary navigation">
        <div className="brand">
          <BrandMark />
          <div>
            <strong>{PRODUCT_NAME}</strong>
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
          <div className="nav-section">
            {navigation.map(({ to, label, short, icon: Icon }) => (
              <NavLink
                aria-label={label}
                className={navLinkClass}
                end={to === "/"}
                key={to}
                to={to}
              >
                <Icon />
                <span className="nav-link-label">{label}</span>
                <span className="nav-link-short">{short}</span>
              </NavLink>
            ))}
          </div>
        </nav>
        <div className="sidebar-footer">
          <NavLink
            aria-label={badge > 0 ? `Activity, ${badge} active` : "Activity"}
            className={navLinkClass}
            to="/tasks"
          >
            <Icons.tasks />
            <span className="nav-link-label">Activity</span>
            <span className="nav-link-short">Activity</span>
            {badge > 0 ? (
              <span aria-hidden="true" className="nav-link-badge">
                {badge}
              </span>
            ) : null}
          </NavLink>
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
            <Route path="/apply" element={<ApplyPage />} />
            <Route path="/applications/:applicationId" element={<ApplicationFilesPage />} />
            <Route path="/find" element={<JobSearchPage />} />
            <Route path="/find/scoring" element={<ScoringPage />} />
            <Route path="/cv" element={<MasterCvPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/welcome" element={<WelcomePage />} />
            {MOVED_ROUTES.map(([from, to]) => (
              <Route element={<Navigate replace to={to} />} key={from} path={from} />
            ))}
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </Suspense>
      </div>
    </div>
  );
}
