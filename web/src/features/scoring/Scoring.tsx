import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  useAllJobs,
  useCreateScoreWeightSet,
  useScoreWeightSets,
  useSearchParameterHistory,
  useSearchParameters,
} from "../../api/hooks";
import type { Job, ScoreWeightsInput } from "../../api/client";
import type { ScoreWeightSet, SearchParametersState } from "../../api/client/types";
import { ActionWithReason } from "../../components/ActionWithReason";
import { Page } from "../../components/Page";
import { ErrorState, LoadingState } from "../../components/States";
import { useWorkspace } from "../../providers";
import { useSearchParameterSave } from "../search-parameters/useSearchParameterSave";

/** Manage scoring weights and their saved versions. */
export function ScoringPage() {
  const { workspaceId } = useWorkspace();
  return (
    <Page
      eyebrow="Decide"
      title="How jobs are scored"
      description="The weights behind an overall score, and every version of the parameters a decision was made under."
    >
      <div className="button-row">
        <Link className="button button--quiet" to="/jobs">
          Back to evaluated jobs
        </Link>
        <Link className="button button--quiet" to="/settings">
          Edit search setup
        </Link>
      </div>
      <SearchPolicy workspaceId={workspaceId} />
      <ScoringWeights workspaceId={workspaceId} />
      <History workspaceId={workspaceId} />
    </Page>
  );
}

/** A search that names no policy scores under the default resolution. */
function policyLabel(name: string) {
  return name || "the default policy";
}

function activeLabel(policy: ScoreWeightSet) {
  return `${policy.name} v${policy.version}${policy.is_shipped_default ? " (shipped default)" : ""}`;
}

/** Choose the named policy the saved search parameters score under. */
function SearchPolicy({ workspaceId }: { workspaceId: string }) {
  const policies = useScoreWeightSets(workspaceId);
  const { stored, save, conflicted, recovery, readLatest, submit, reset, keepLatest } =
    useSearchParameterSave(workspaceId);
  const [choice, setChoice] = useState<string | null>(null);

  if (stored.isLoading) return <LoadingState label="Reading your search parameters" />;
  if (stored.isError && !stored.data) return <ErrorState error={stored.error} />;

  const inForce = stored.data?.parameters.score_weight_set ?? "";
  const selected = choice ?? inForce;
  const names = [...new Set((policies.data ?? []).map((item) => item.name))];
  // A stored name missing from the list stays selectable rather than silently dropped.
  const missing = [...new Set([inForce, selected])].filter((name) => name && !names.includes(name));
  const options = [...missing, ...names];
  const active = policies.data?.find((item) => item.is_active);
  const latestPolicy = recovery?.latest?.parameters.score_weight_set ?? "";
  const alreadyLatest = Boolean(recovery?.latest) && latestPolicy === selected;
  const saveAgainst = (state: SearchParametersState) =>
    submit({ ...state.parameters, score_weight_set: selected }, state.version, () =>
      setChoice(null),
    );
  const dismiss = () => {
    setChoice(null);
    reset();
  };

  return (
    <article className="panel">
      <header>
        <div>
          <h2>Policy for your search</h2>
          <p>
            Choosing a policy saves a new version of your search parameters with only this choice
            changed. New evaluations use it; jobs already scored keep their recorded scores.
          </p>
        </div>
      </header>
      <div className="panel-body">
        <div className="button-row">
          <label className="compact-field">
            Scoring policy for this search
            <select
              onChange={(event) => {
                setChoice(event.target.value);
                if (!conflicted) save.reset();
              }}
              value={selected}
            >
              <option value="">Default policy (newest in this workspace, else shipped)</option>
              {options.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <ActionWithReason
            disabledReason={
              conflicted
                ? recovery?.latest
                  ? `Choose below whether to apply it to version ${recovery.latest.version}`
                  : "Read the latest saved parameters first"
                : selected === inForce
                  ? "This policy is already in force"
                  : undefined
            }
            onClick={() => stored.data && saveAgainst(stored.data)}
            pending={save.isPending}
          >
            {save.isPending ? "Saving" : "Use this policy"}
          </ActionWithReason>
        </div>
        {/* Hidden while the latest read is pending or failed, since the cached version may be stale. */}
        {!recovery?.reading && !recovery?.error && (
          <p className="field-note">
            In force now:{" "}
            {inForce
              ? `${inForce}${active?.name === inForce ? ` v${active.version}` : ""}`
              : `${policyLabel("")}, which resolves to ${active ? activeLabel(active) : "no stored policy"}`}
            .
          </p>
        )}
        {policies.isError && (
          <p className="field-error">
            Could not read the scoring policies: {policies.error.message} Only the saved choice is
            listed.
          </p>
        )}
        {save.isSuccess && !save.data.unchanged && (
          <p role="status">
            Saved as search parameters version {save.data.version}. The search now uses{" "}
            {policyLabel(save.data.parameters.score_weight_set ?? "")}.
          </p>
        )}
        {save.data?.unchanged && (
          <p role="status">Identical to version {save.data.version}; nothing was written.</p>
        )}
        {save.isError && !conflicted && <p className="field-error">{save.error.message}</p>}
        {conflicted && (
          <div role="alert">
            <p>
              Your search parameters changed elsewhere since this page read them, so nothing was
              saved. Read the latest version, then choose whether to apply {policyLabel(selected)}{" "}
              to it.
            </p>
            {alreadyLatest ? (
              <div className="button-row">
                <button className="button button--quiet" onClick={dismiss} type="button">
                  Dismiss
                </button>
              </div>
            ) : (
              <div className="button-row">
                <button
                  className="button button--quiet"
                  disabled={recovery?.reading}
                  onClick={() => void readLatest()}
                  type="button"
                >
                  {recovery?.error
                    ? "Retry reading latest parameters"
                    : "Read latest saved parameters"}
                </button>
                {recovery?.latest && (
                  <button
                    className="button button--primary"
                    onClick={() => recovery.latest && saveAgainst(recovery.latest)}
                    type="button"
                  >
                    Use {policyLabel(selected)} against version {recovery.latest.version}
                  </button>
                )}
                <button
                  className="button button--quiet"
                  disabled={recovery?.reading}
                  onClick={() =>
                    void keepLatest().then((kept) => {
                      if (kept) setChoice(null);
                    })
                  }
                  type="button"
                >
                  Keep the saved policy
                </button>
              </div>
            )}
            {recovery?.reading && <p role="status">Reading the latest saved parameters.</p>}
            {recovery?.error && <p>Could not read the latest parameters: {recovery.error}</p>}
            {recovery?.latest && (
              <p>
                Saved version {recovery.latest.version} {alreadyLatest ? "already uses" : "uses"}{" "}
                {policyLabel(latestPolicy)}.
              </p>
            )}
          </div>
        )}
      </div>
    </article>
  );
}

function History({ workspaceId }: { workspaceId: string }) {
  const history = useSearchParameterHistory(workspaceId);
  if (history.isLoading) return <LoadingState label="Reading history" />;
  if (history.isError) return <ErrorState error={history.error} />;
  return (
    <article className="panel">
      <header>
        <div>
          <h2>Version history</h2>
          <p>
            Parameters are appended, never overwritten, so a decision made last month can still be
            read against the parameters that were in force last month.
          </p>
        </div>
      </header>
      <div className="panel-body">
        {!history.data?.length ? (
          <p className="field-note">No versions yet. Saving writes version 1.</p>
        ) : (
          <div className="table-wrap">
            <table aria-label="Search parameter version history">
              <thead>
                <tr>
                  <th scope="col">Version</th>
                  <th scope="col">Effective</th>
                  <th scope="col">Saved by</th>
                  <th scope="col">Origin</th>
                  <th scope="col">Digest</th>
                </tr>
              </thead>
              <tbody>
                {history.data.map((version) => (
                  <tr key={version.id}>
                    <td>{version.version}</td>
                    <td>{new Date(version.effective_at).toLocaleString()}</td>
                    <td>{version.created_by}</td>
                    <td>{version.origin}</td>
                    <td className="path-value">{version.content_sha256.slice(0, 12)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </article>
  );
}

const WEIGHT_COMPONENTS = [
  { name: "fit", label: "Fit", score: "fit_score" },
  { name: "compensation", label: "Compensation", score: "compensation_score" },
  { name: "eligibility", label: "Eligibility", score: "eligibility_score" },
  { name: "opportunity", label: "Opportunity", score: "opportunity_score" },
  { name: "evidence", label: "Evidence (confidence)", score: "evidence_score" },
] as const;

type WeightDraft = Partial<Record<keyof ScoreWeightsInput, string>>;

/** Two decimals everywhere, so what the browser sums is what the server sums. */
function toWeight(value: string): number {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? Math.round(parsed * 100) / 100 : 0;
}

function weightTotal(draft: WeightDraft): number {
  return (
    Math.round(
      WEIGHT_COMPONENTS.reduce((sum, c) => sum + toWeight(draft[c.name] ?? "0"), 0) * 100,
    ) / 100
  );
}

function authoredWeights(draft: WeightDraft): ScoreWeightsInput {
  return {
    fit: toWeight(draft.fit ?? "0").toFixed(2),
    compensation: toWeight(draft.compensation ?? "0").toFixed(2),
    eligibility: toWeight(draft.eligibility ?? "0").toFixed(2),
    evidence: toWeight(draft.evidence ?? "0").toFixed(2),
    opportunity: toWeight(draft.opportunity ?? "0").toFixed(2),
  };
}

/**
 * Recompute one job's overall under proposed weights.
 *
 * The same rule the server applies: a component with zero weight
 * is not part of the composite and cannot suppress one, and a weighted
 * component that abstains makes the whole total unstated.
 */
function overallUnder(job: Job, draft: WeightDraft): number | null {
  let total = 0;
  for (const component of WEIGHT_COMPONENTS) {
    const weight = toWeight(draft[component.name] ?? "0");
    if (weight <= 0) continue;
    const value = job[component.score];
    if (value == null) return null;
    total += value * weight;
  }
  return Math.round(total);
}

function ScoringWeights({ workspaceId }: { workspaceId: string }) {
  const policies = useScoreWeightSets(workspaceId);
  const searchPolicy = useSearchParameters(workspaceId).data?.parameters.score_weight_set ?? "";
  const create = useCreateScoreWeightSet(workspaceId, searchPolicy);
  const allJobs = useAllJobs(workspaceId);
  const [draft, setDraft] = useState<WeightDraft | null>(null);
  const [name, setName] = useState("");

  const active = policies.data?.find((item) => item.is_active);
  const current: WeightDraft = useMemo(() => {
    const source = active?.weights ?? [];
    return Object.fromEntries(source.map((item) => [item.component, item.weight.toFixed(2)]));
  }, [active]);
  const working = draft ?? current;
  const total = weightTotal(working);
  const balanced = total === 1;

  const preview = useMemo(() => {
    const jobs = allJobs.data ?? [];
    const before = [...jobs].sort((a, b) => (b.overall_score ?? -1) - (a.overall_score ?? -1));
    const rank = new Map(before.map((job, index) => [job.job_posting_id, index]));
    const after = jobs
      .map((job) => ({ job, score: overallUnder(job, working) }))
      .sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
    return after.slice(0, 10).map((entry, index) => ({
      ...entry,
      movement: (rank.get(entry.job.job_posting_id) ?? index) - index,
    }));
  }, [allJobs.data, working]);

  if (policies.isLoading) return <LoadingState label="Reading scoring policies" />;
  if (policies.isError) return <ErrorState error={policies.error} />;

  return (
    <article className="panel">
      <header>
        <div>
          <h2>Scoring weights</h2>
          <p>
            How much pay matters relative to fit is the most consequential setting here. Weights are
            versioned and never edited: saving appends a policy, and evaluations already committed
            keep the weights they were scored under.
          </p>
        </div>
      </header>
      <div className="panel-body">
        <p className="field-note">In force now: {active ? activeLabel(active) : "none"}.</p>

        <div className="weight-grid">
          {WEIGHT_COMPONENTS.map((component) => (
            <label className="compact-field" key={component.name}>
              {component.label}
              <input
                max="1"
                min="0"
                onChange={(event) => setDraft({ ...working, [component.name]: event.target.value })}
                step="0.01"
                type="number"
                value={working[component.name] ?? "0.00"}
              />
            </label>
          ))}
        </div>

        <p className={balanced ? "field-note" : "field-error"}>
          Total {total.toFixed(2)}.{" "}
          {balanced
            ? "A component at 0.00 is excluded from the overall and shown as confidence instead."
            : "Weights have to sum to exactly 1.00 before this can be saved."}
        </p>

        <div className="button-row">
          <label className="compact-field">
            Policy name
            <input
              onChange={(event) => setName(event.target.value)}
              placeholder="For example: Pay first"
              value={name}
            />
          </label>
          <ActionWithReason
            disabledReason={
              !balanced
                ? "The weights must total 1.00"
                : !name.trim()
                  ? "Name the policy to save it"
                  : undefined
            }
            onClick={() =>
              create.mutate(
                {
                  name: name.trim(),
                  weights: authoredWeights(working),
                },
                {
                  onSuccess: () => {
                    setDraft(null);
                    setName("");
                  },
                },
              )
            }
            pending={create.isPending}
          >
            {create.isPending ? "Saving" : "Save as a new policy"}
          </ActionWithReason>
        </div>
        {create.isError && <p className="field-error">{create.error.message}</p>}

        <h3>How this would reorder your evaluated jobs</h3>
        {allJobs.isLoading && <LoadingState label="Reading evaluated jobs" />}
        {allJobs.isError && <ErrorState error={allJobs.error} />}
        {allJobs.data && (
          <>
            <p className="field-note">
              Previewing the top 10 of {allJobs.data.length} evaluated{" "}
              {allJobs.data.length === 1 ? "job" : "jobs"}. This is a preview only. Nothing is
              rescored, and saving a policy changes new evaluations rather than these.
            </p>
            <div className="table-wrap">
              <table aria-label="Score policy ranking preview">
                <thead>
                  <tr>
                    <th scope="col">Employer</th>
                    <th scope="col">Role</th>
                    <th scope="col">Recorded</th>
                    <th scope="col">Under these weights</th>
                    <th scope="col">Move</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.map((entry) => (
                    <tr key={entry.job.job_posting_id}>
                      <th scope="row">{entry.job.employer}</th>
                      <td>{entry.job.title}</td>
                      <td className="score-cell">{entry.job.overall_score ?? "-"}</td>
                      <td className="score-cell">{entry.score ?? "-"}</td>
                      <td className="score-cell">
                        {entry.movement === 0
                          ? "no change"
                          : entry.movement > 0
                            ? `up ${entry.movement}`
                            : `down ${-entry.movement}`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}

        <h3>Stored policies</h3>
        <ul className="policy-list">
          {policies.data?.map((item) => (
            <li key={item.id}>
              <strong>
                {item.name} v{item.version}
              </strong>
              {item.is_active && <span className="status status--tone-positive">In force</span>}
              {item.is_shipped_default && (
                <span className="status status--tone-neutral">Shipped</span>
              )}
              <small>
                {item.weights
                  .filter((w) => w.weight > 0)
                  .map((w) => `${w.component} ${w.weight.toFixed(2)}`)
                  .join(", ")}
              </small>
            </li>
          ))}
        </ul>
      </div>
    </article>
  );
}
