import { useMemo } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";

import type { Guardrails } from "../../api/client";
import {
  useGuardrails,
  useGuardrailHistory,
  useHealth,
  useSaveGuardrails,
  useWorkspaces,
} from "../../api/hooks";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { Page } from "../../components/Page";
import { ErrorState, LoadingState } from "../../components/States";
import { TabPanel, Tabs } from "../../components/Tabs";
import { ApiPlaygroundPage } from "../../pages/ApiPlaygroundPage";
import { PromptsPage } from "../../pages/PromptsPage";
import { SpendPage } from "../../pages/SpendPage";
import { WorkspacePage } from "../../pages/WorkspacePage";
import { PRODUCT_NAME } from "../../productName";
import { PostingRefusals } from "../job-search/PostingRefusals";
import { SearchParameterSetup } from "../search-parameters/SearchParameterSetup";
import { GuardrailPreviewPanel } from "./GuardrailPreviewPanel";
import { LearningLoopMetricsPanel } from "./LearningLoopMetricsPanel";
import { ProviderPanel } from "./ProviderPanel";
import { RunCostHistoryPanel } from "./RunCostHistoryPanel";
import { useWorkspaceDraft } from "../../hooks/useWorkspaceDraft";
import { useWorkspace } from "../../providers";

const settingsTabs = [
  { id: "search", label: "Search" },
  { id: "provider", label: "Provider" },
  { id: "drafting", label: "Drafting" },
  { id: "costs", label: "Costs" },
  { id: "records", label: "Notes & tags" },
  { id: "advanced", label: "Advanced" },
] as const;
type SettingsTab = (typeof settingsTabs)[number]["id"];

const advancedSections = [
  { id: "prompts", label: "Prompts" },
  { id: "diagnostics", label: "Diagnostics" },
  { id: "refusals", label: "Posting refusals" },
] as const;
type AdvancedSection = (typeof advancedSections)[number]["id"];

function isSettingsTab(value: string | null): value is SettingsTab {
  return settingsTabs.some((item) => item.id === value);
}

function isAdvancedSection(value: string | null): value is AdvancedSection {
  return advancedSections.some((item) => item.id === value);
}

/** Where an address from before the Settings sections were regrouped now points. */
function movedSettingsAddress(params: URLSearchParams): string | null {
  const tab = params.get("tab");
  if (tab === "search" && params.get("section") === "sources") return "/find?tab=boards";
  if (tab === "workspace") return "/settings?tab=provider";
  if (tab === "signals") return "/settings?tab=advanced&section=diagnostics";
  return null;
}

export function SettingsPage() {
  const { workspaceId } = useWorkspace();
  const health = useHealth();
  const workspaces = useWorkspaces();
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("tab");
  const tab: SettingsTab = isSettingsTab(requested) ? requested : "search";
  const moved = movedSettingsAddress(searchParams);
  const setTab = (value: SettingsTab) => {
    const next = new URLSearchParams();
    if (value !== "search") next.set("tab", value);
    setSearchParams(next, { replace: true });
  };
  if (moved) return <Navigate replace to={moved} />;
  if (health.isLoading || workspaces.isLoading)
    return <LoadingState label="Checking local health" />;
  if (health.isError || workspaces.isError)
    return <ErrorState error={health.error ?? workspaces.error} />;
  return (
    <Page
      eyebrow="Settings"
      title="Settings"
      description="Set up your search, your provider key, and what runs may cost."
    >
      <Tabs
        id="settings-tabs"
        items={settingsTabs}
        label="Settings sections"
        onSelect={(id) => setTab(id as SettingsTab)}
        selected={tab}
      />
      <TabPanel id="settings-tabs" selected={tab}>
        {tab === "search" && <SearchParameterSetup workspaceId={workspaceId} />}
        {tab === "provider" && <ProviderTab />}
        {tab === "drafting" && <DraftingTab workspaceId={workspaceId} />}
        {tab === "costs" && <CostsTab workspaceId={workspaceId} />}
        {tab === "records" && <WorkspacePage />}
        {tab === "advanced" && <AdvancedTab workspaceId={workspaceId} />}
      </TabPanel>
    </Page>
  );
}

function ProviderTab() {
  return (
    <section className="settings-grid">
      <ProviderPanel />
      <article className="panel privacy-card">
        <p className="eyebrow">Privacy boundary</p>
        <h2>Credentials stay behind the API.</h2>
        <p>
          A key is typed once and posted once. The browser is told only whether one is present; the
          value itself lives in your system keyring and is never returned, logged, or written to the
          database.
        </p>
        <ul>
          <li>Local loopback binding</li>
          <li>Private database and artifact permissions</li>
          <li>No arbitrary posting HTML</li>
          <li>No automatic application submission</li>
          <li>No document content in browser storage</li>
        </ul>
      </article>
      <article className="panel">
        <header>
          <div>
            <h2>Setup walkthrough</h2>
            <p>Return to free setup and review optional paid features.</p>
          </div>
        </header>
        <div className="panel-body">
          <Link className="button button--quiet" to="/welcome">
            Open setup walkthrough
          </Link>
        </div>
      </article>
    </section>
  );
}

function CostsTab({ workspaceId }: { workspaceId: string }) {
  return (
    <>
      <SpendPage />
      <RunCostHistoryPanel workspaceId={workspaceId} />
    </>
  );
}

function AdvancedTab({ workspaceId }: { workspaceId: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = searchParams.get("section");
  const section: AdvancedSection = isAdvancedSection(requested) ? requested : "prompts";
  const setSection = (value: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("section", value);
    setSearchParams(next, { replace: true });
  };
  return (
    <>
      <Tabs
        id="settings-advanced-tabs"
        items={advancedSections}
        label="Advanced sections"
        onSelect={setSection}
        selected={section}
      />
      <TabPanel id="settings-advanced-tabs" selected={section}>
        {section === "prompts" && <PromptsPage />}
        {section === "diagnostics" && <DiagnosticsSection workspaceId={workspaceId} />}
        {section === "refusals" && <PostingRefusals workspaceId={workspaceId} />}
      </TabPanel>
    </>
  );
}

function DiagnosticsSection({ workspaceId }: { workspaceId: string }) {
  const health = useHealth();
  const workspaces = useWorkspaces();
  const workspace = workspaces.data?.find((item) => item.id === workspaceId);
  return (
    <>
      <section className="settings-grid">
        <article className="panel health-card">
          <header>
            <div>
              <h2>Service health</h2>
              <p>
                {PRODUCT_NAME} {health.data?.version}
              </p>
            </div>
            <span className={`health-dot ${health.data?.status === "ok" ? "healthy" : ""}`}>
              <i />
              {health.data?.status}
            </span>
          </header>
          <dl className="settings-list">
            <div>
              <dt>Database</dt>
              <dd>{health.data?.database_reachable ? "Reachable" : "Unavailable"}</dd>
            </div>
            <div>
              <dt>Schema revision</dt>
              <dd>{health.data?.current_revision ?? "None"}</dd>
            </div>
            <div>
              <dt>Schema current</dt>
              <dd>{health.data?.schema_current ? "Yes" : "No, run an explicit upgrade"}</dd>
            </div>
            <div>
              <dt>Background worker</dt>
              <dd>{health.data?.worker_available ? "Running" : "Not running"}</dd>
            </div>
          </dl>
        </article>
        <article className="panel">
          <header>
            <div>
              <h2>Workspace</h2>
              <p>User-owned data boundary</p>
            </div>
          </header>
          <dl className="settings-list">
            <div>
              <dt>Name</dt>
              <dd>{workspace?.name}</dd>
            </div>
            <div>
              <dt>Short name</dt>
              <dd>{workspace?.slug}</dd>
            </div>
            <div>
              <dt>Documents folder</dt>
              <dd className="path-value">{workspace?.root_path ?? "Not linked"}</dd>
            </div>
            <div>
              <dt>Workspace ID</dt>
              <dd className="path-value">{workspace?.id}</dd>
            </div>
          </dl>
        </article>
        <article className="panel">
          <header>
            <div>
              <h2>Diagnostic tools</h2>
              <p>For when something has gone wrong.</p>
            </div>
          </header>
          <ul className="record-list">
            <li>
              <Link to="/tasks">Activity</Link>
              <small>Every background task, and why any of them failed.</small>
            </li>
            <li>
              {/* A server-rendered page outside the router, so not a Link. */}
              <a href="/admin/">Data browser</a>
              <small>Read the local database table by table.</small>
            </li>
          </ul>
        </article>
        <LearningLoopMetricsPanel workspaceId={workspaceId} />
      </section>
      <ApiPlaygroundPage />
    </>
  );
}

function DraftingTab({ workspaceId }: { workspaceId: string }) {
  return (
    <>
      <GuardrailsEditor workspaceId={workspaceId} />
      <GuardrailHistory workspaceId={workspaceId} />
    </>
  );
}

type GuardrailDraft = Omit<Guardrails, "protected" | "required" | "forbidden" | "distinctions"> & {
  protected: string;
  required: string;
  forbidden: string;
  distinctions: string;
};

function ruleLines(values: string[]): string {
  return values.join("\n");
}

function parseRuleLines(value: string): string[] {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function GuardrailsEditor({ workspaceId }: { workspaceId: string }) {
  const guardrails = useGuardrails(workspaceId);
  const save = useSaveGuardrails(workspaceId);
  const { edits, setEdits, expectedVersion, storageError } = useWorkspaceDraft<GuardrailDraft>(
    workspaceId,
    "guardrails",
    guardrails.data?.version ?? 0,
  );
  const saved = useMemo<GuardrailDraft | null>(() => {
    if (!guardrails.data) return null;
    return {
      protected: ruleLines(guardrails.data.rules.protected ?? []),
      required: ruleLines(guardrails.data.rules.required ?? []),
      forbidden: ruleLines(guardrails.data.rules.forbidden ?? []),
      work_authorization: guardrails.data.rules.work_authorization ?? null,
      distinctions: ruleLines(guardrails.data.rules.distinctions ?? []),
      first_person: guardrails.data.rules.first_person ?? false,
      ascii_only: guardrails.data.rules.ascii_only ?? true,
    };
  }, [guardrails.data]);
  if (guardrails.isLoading) return <LoadingState label="Reading claim guardrails" />;
  if (guardrails.isError || !guardrails.data || !saved) {
    return (
      <ErrorState error={guardrails.error ?? new Error("Claim guardrails are unavailable.")} />
    );
  }
  const draft = edits ?? saved;
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const patch = (changes: Partial<GuardrailDraft>) => setEdits({ ...draft, ...changes });
  const rules: Guardrails = {
    protected: parseRuleLines(draft.protected),
    required: parseRuleLines(draft.required),
    forbidden: parseRuleLines(draft.forbidden),
    work_authorization: draft.work_authorization?.trim() || null,
    distinctions: parseRuleLines(draft.distinctions),
    first_person: draft.first_person,
    ascii_only: draft.ascii_only,
  };
  return (
    <section className="panel guardrails-panel" aria-labelledby="guardrails-heading">
      <header>
        <div>
          <h2 id="guardrails-heading">Claim guardrails</h2>
          <p>
            These rules constrain every generated CV. They can stop unsupported wording, but cannot
            add a fact to your record or make a note into evidence.
          </p>
        </div>
        <span className="field-note">
          Version {guardrails.data.version}
          {guardrails.data.configured ? ", configured" : ", not configured"}
        </span>
      </header>
      <div className="panel-body">
        <p className="field-note">
          Enter one phrase or distinction per line. Blank lines are ignored.
        </p>
        <details>
          <summary>How rules match</summary>
          <dl className="settings-list">
            <div>
              <dt>Protected</dt>
              <dd>
                Protecting &quot;Northwind Analytics&quot; also matches &quot;northwind
                &nbsp;&nbsp;analytics&quot; (extra spaces, any case), and then requires the resume
                to spell it exactly &quot;Northwind Analytics&quot; wherever that appears.
              </dd>
            </div>
            <div>
              <dt>Required</dt>
              <dd>
                Requiring &quot;PMP certification&quot; fails the check if the resume never contains
                that exact text, even when a synonym appears instead.
              </dd>
            </div>
            <div>
              <dt>Forbidden</dt>
              <dd>
                Forbidding &quot;world-class&quot; fails the check on &quot;world-class&quot;,
                &quot;World-class&quot;, and &quot;World-Class&quot;. It does not fire inside a
                longer word, so forbidding &quot;RAG&quot; leaves &quot;storage&quot; alone.
              </dd>
            </div>
            <div>
              <dt>Distinctions</dt>
              <dd>
                Matching, resume and letter drafting, and the model critic all read these rules.
                &quot;Led delivery; did not own the platform&quot; limits how that evidence may
                answer a requirement. The rule is interpreted by a model, not enforced as an
                exact-text check.
              </dd>
            </div>
            <div>
              <dt>Work authorization</dt>
              <dd>
                Setting this to &quot;Authorized to work in Canada.&quot; replaces any authorization
                sentence the master CV&apos;s contact block already states.
              </dd>
            </div>
          </dl>
        </details>
        <div className="guardrails-fields">
          <label className="guardrails-field">
            <span className="guardrails-field__label">Protected phrases</span>
            <textarea
              aria-describedby="protected-note"
              aria-label="Protected phrases"
              onChange={(event) => patch({ protected: event.target.value })}
              rows={4}
              value={draft.protected}
            />
            <small className="field-note" id="protected-note">
              Case-insensitive match with whitespace collapsed. If the phrase (or an equivalent
              spacing) appears, it must be written verbatim wherever it appears.
            </small>
          </label>
          <label className="guardrails-field">
            <span className="guardrails-field__label">Required phrases</span>
            <textarea
              aria-describedby="required-note"
              aria-label="Required phrases"
              onChange={(event) => patch({ required: event.target.value })}
              rows={4}
              value={draft.required}
            />
            <small className="field-note" id="required-note">
              Case-sensitive substring match. Each phrase must appear at least once in the drafted
              resume; approval refuses regeneration when one is missing.
            </small>
          </label>
          <label className="guardrails-field">
            <span className="guardrails-field__label">Forbidden phrases</span>
            <textarea
              aria-describedby="forbidden-note"
              aria-label="Forbidden phrases"
              onChange={(event) => patch({ forbidden: event.target.value })}
              rows={4}
              value={draft.forbidden}
            />
            <small className="field-note" id="forbidden-note">
              Matched in any case, as whole words, with runs of whitespace collapsed. Any occurrence
              in the drafted resume or cover letter fails the check.
            </small>
          </label>
          <label className="guardrails-field">
            <span className="guardrails-field__label">Distinctions</span>
            <textarea
              aria-label="Distinctions"
              onChange={(event) => patch({ distinctions: event.target.value })}
              rows={4}
              value={draft.distinctions}
            />
            <small className="field-note">
              Matching uses these distinctions to decide whether evidence answers a requirement,
              including credential equivalence or material differences. Resume and letter drafting
              and the model critic also read them. These are model instructions, not deterministic
              proof. Blank adds no applicant-specific distinctions; existing evidence and shipped
              matching rules still apply. Save guardrails creates a workspace version for later
              runs.
            </small>
          </label>
          <label className="guardrails-field guardrails-field--full">
            <span className="guardrails-field__label">Work authorization line</span>
            <input
              aria-describedby="work-authorization-note"
              aria-label="Work authorization line"
              onChange={(event) => patch({ work_authorization: event.target.value })}
              value={draft.work_authorization ?? ""}
            />
            <small className="field-note" id="work-authorization-note">
              Replaces any work-authorization line found in the master CV&apos;s contact block.
              Leave blank to keep the CV&apos;s line.
            </small>
          </label>
          <div className="guardrails-toggles">
            <label>
              <input
                checked={draft.first_person}
                onChange={(event) => patch({ first_person: event.target.checked })}
                type="checkbox"
              />
              Allow first person
            </label>
            <label>
              <input
                checked={draft.ascii_only}
                onChange={(event) => patch({ ascii_only: event.target.checked })}
                type="checkbox"
              />
              Require ASCII punctuation
            </label>
          </div>
        </div>
        <GuardrailPreviewPanel rules={rules} workspaceId={workspaceId} />
        {storageError && (
          <p role="alert">
            Browser draft storage is unavailable. Save your changes before leaving this form.
          </p>
        )}
        {save.isSuccess && !dirty && <p role="status">Guardrails saved.</p>}
        {dirty && (
          <p role="status">Unsaved changes - kept in this browser tab until saved or discarded.</p>
        )}
        {save.isError && <p className="field-error">{save.error.message}</p>}
        <div className="button-row">
          <ConfirmedActionButton
            className="button button--danger"
            disabled={!dirty || save.isPending}
            onConfirm={() => setEdits(null)}
            options={{
              title: "Discard these guardrail changes?",
              description:
                "The current saved guardrails stay in force. Changes made since the last save are lost.",
              confirmLabel: "Discard changes",
              danger: true,
            }}
          >
            Discard changes
          </ConfirmedActionButton>
          <button
            className="button button--primary"
            disabled={!dirty || save.isPending}
            onClick={() =>
              save.mutate(
                { expected_version: expectedVersion, rules },
                { onSuccess: () => setEdits(null) },
              )
            }
            type="button"
          >
            {save.isPending ? "Saving" : "Save guardrails"}
          </button>
        </div>
      </div>
    </section>
  );
}

function GuardrailHistory({ workspaceId }: { workspaceId: string }) {
  const history = useGuardrailHistory(workspaceId);
  if (history.isLoading) return <LoadingState label="Reading guardrail history" />;
  if (history.isError) return <ErrorState error={history.error} />;
  return (
    <section className="panel" aria-labelledby="guardrail-history-heading">
      <header>
        <div>
          <h2 id="guardrail-history-heading">Guardrail history</h2>
          <p>Every version records the anti-fabrication rules that were in force.</p>
        </div>
      </header>
      {history.data?.length ? (
        <ol className="record-list">
          {history.data.map((version) => (
            <li key={version.id}>
              <strong>Version {version.version}</strong>
              <small>
                Effective {new Date(version.effective_at).toLocaleString()} from {version.origin}.
              </small>
              <details>
                <summary>Rules in force</summary>
                <pre>{JSON.stringify(version.rules, null, 2)}</pre>
              </details>
            </li>
          ))}
        </ol>
      ) : (
        <p className="field-note">No guardrail version has been saved in this workspace.</p>
      )}
    </section>
  );
}
