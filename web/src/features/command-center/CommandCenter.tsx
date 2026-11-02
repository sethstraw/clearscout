import type { ReactNode } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Link } from "react-router-dom";

import { useTasks, useWorkspaceSummary } from "../../api/hooks";
import type { Job } from "../../api/client";
import { Page } from "../../components/Page";
import { reviewScores } from "../../components/reviewScores";
import { EvaluationInputs } from "../../components/EvaluationInputs";
import { attemptCount, taskKindOf } from "../../api/taskNames";
import { StatusBadge } from "../../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { useWorkspace } from "../../providers";
import { DecidedWithoutOutcome } from "./DecidedWithoutOutcome";
import { FirstRunChecklist } from "./FirstRunChecklist";

function JobList({
  title,
  detail,
  emptyMessage = "No jobs in this category yet.",
  jobs,
  scoreKey,
  scoreLabel,
  showApplicationStatus = false,
}: {
  title: string;
  detail: string;
  emptyMessage?: string;
  jobs: Job[];
  scoreKey: "fit_score" | "overall_score";
  scoreLabel: string;
  showApplicationStatus?: boolean;
}) {
  return (
    <section className="panel ranked-list">
      <header>
        <div>
          <h2>{title}</h2>
          <p>{detail}</p>
        </div>
      </header>
      {jobs.length ? (
        jobs.slice(0, 4).map((job) => {
          const score = reviewScores(job)[scoreKey === "fit_score" ? "fit" : "overall"];
          return (
            <Link
              className="ranked-row"
              key={job.job_posting_id}
              to={`/jobs/${job.job_posting_id}`}
            >
              <div>
                <strong>{job.title}</strong>
                <span>{job.employer}</span>
                <EvaluationInputs
                  evaluated={job.evaluation_id !== null}
                  inputs={job.evaluation_inputs}
                />
              </div>
              <div className="ranked-values">
                {showApplicationStatus ? <StatusBadge value={job.application_status} /> : null}
                <div className="ranked-score">
                  <strong>{score.value ?? "Not scored"}</strong>
                  <span>
                    {scoreLabel}
                    {score.source === "review" ? " (your review)" : ""}
                    {score.source === "minimum" ? " (minimum)" : ""}
                  </span>
                </div>
              </div>
            </Link>
          );
        })
      ) : (
        <EmptyState>{emptyMessage}</EmptyState>
      )}
    </section>
  );
}

function MetricCard({
  label,
  value,
  detail,
  accent = "violet",
}: {
  label: string;
  value: ReactNode;
  detail: string;
  accent?: "violet" | "teal" | "amber" | "blue";
}) {
  return (
    <article className={`metric metric--${accent}`}>
      <p>{label}</p>
      <strong>{value}</strong>
      <span>{detail}</span>
    </article>
  );
}

export function CommandCenterPage() {
  const { workspaceId } = useWorkspace();
  const summary = useWorkspaceSummary(workspaceId);
  const tasks = useTasks(workspaceId);
  if (summary.isLoading) return <LoadingState label="Opening Home" />;
  if (summary.isError) return <ErrorState error={summary.error} />;
  const activeTasks = (tasks.data ?? []).filter((task) =>
    ["queued", "running", "needs_review", "committing"].includes(task.status),
  );
  const actionableApplications = summary.data?.actionable_applications ?? 0;
  const funnel = [
    { label: "Ready", count: summary.data?.application_funnel.package_ready ?? 0 },
    { label: "Applied", count: summary.data?.application_funnel.applied ?? 0 },
    { label: "Interview", count: summary.data?.application_funnel.interviewing ?? 0 },
    { label: "Offer", count: summary.data?.application_funnel.offer ?? 0 },
  ];
  return (
    <Page
      eyebrow="Today"
      title="Home"
      description="What needs you today."
      action={
        <Link
          className="button button--primary"
          to={actionableApplications > 0 ? "/apply" : "/jobs"}
        >
          {actionableApplications > 0 ? "Review applications" : "Explore jobs"}
        </Link>
      }
    >
      <FirstRunChecklist workspaceId={workspaceId} />
      <DecidedWithoutOutcome
        count={summary.data?.decided_without_outcome_count ?? 0}
        jobs={summary.data?.decided_without_outcome ?? []}
        workspaceId={workspaceId}
      />
      <section className="metric-grid" aria-label="Job search overview">
        <MetricCard
          label="Tracked postings"
          value={summary.data?.total_jobs ?? 0}
          detail="Job postings in the ledger, archived ones excluded"
        />
        <MetricCard
          accent="teal"
          label="Actionable applications"
          value={actionableApplications}
          detail="Ready, submitted, or advancing"
        />
        <MetricCard
          accent="amber"
          label="Open tasks"
          value={activeTasks.length}
          detail="Queued, running, or awaiting review"
        />
        <MetricCard
          accent="blue"
          label="Incomplete evidence"
          value={summary.data?.incomplete_jobs ?? 0}
          detail="Posting, evidence, report, or identity gaps"
        />
      </section>
      <section className="dashboard-grid">
        <JobList
          title="Waiting on you"
          detail="Applications you are pursuing, most advanced first."
          jobs={summary.data?.actionable ?? []}
          scoreKey="overall_score"
          scoreLabel="overall"
          showApplicationStatus
        />
        <JobList
          title="Best fit with your CV"
          detail="Evaluated jobs with a fit score. Your fit review when entered; otherwise candidate evidence and role fit."
          emptyMessage="No evaluated jobs with a fit score yet."
          jobs={(summary.data?.highest_intrinsic ?? []).filter(
            (job) => job.evaluation_id !== null && reviewScores(job).fit.value !== null,
          )}
          scoreKey="fit_score"
          scoreLabel="fit"
        />
        <JobList
          title="Promising jobs to revisit"
          detail="Jobs marked investigate or monitor. Highest overall score first; jobs with no overall score come last, by employer then title."
          jobs={summary.data?.highest_upside ?? []}
          scoreKey="overall_score"
          scoreLabel="overall"
        />
        <section className="panel chart-panel">
          <header>
            <div>
              <h2>Application pipeline</h2>
              <p>Each move appends history; nothing is overwritten.</p>
            </div>
          </header>
          <div
            className="chart"
            role="img"
            aria-label={`Application pipeline: ${funnel.map((item) => `${item.label} ${item.count}`).join(", ")}`}
          >
            <ResponsiveContainer height="100%" width="100%">
              <BarChart data={funnel} margin={{ right: 12 }}>
                <CartesianGrid strokeDasharray="4 4" vertical={false} />
                <XAxis dataKey="label" fontSize={12} interval={0} />
                <YAxis allowDecimals={false} fontSize={12} width={24} />
                <Tooltip cursor={{ fill: "var(--surface-muted)" }} />
                <Bar dataKey="count" fill="var(--accent)" radius={[6, 6, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>
        <section className="panel task-panel">
          <header>
            <div>
              <h2>Recent tasks</h2>
              <p>Durable work survives a restart.</p>
            </div>
            <Link to="/tasks">View all</Link>
          </header>
          {(tasks.data ?? []).slice(0, 5).map((task) => (
            <div className="task-row" key={task.id}>
              <div>
                <strong>{taskKindOf(task).name}</strong>
                <span>{attemptCount(task.attempt_count)}</span>
                {task.error_message ? <p className="task-error">{task.error_message}</p> : null}
              </div>
              <StatusBadge value={task.status} />
            </div>
          ))}
          {!tasks.data?.length && <EmptyState>No background work has run yet.</EmptyState>}
        </section>
      </section>
    </Page>
  );
}
