import { useState } from "react";
import { useForm } from "react-hook-form";
import { Link, useNavigate } from "react-router-dom";

import type { Provider } from "../../api/client";
import {
  useInitializeWorkspace,
  useProviders,
  useSearchParameters,
  useWorkspaces,
} from "../../api/hooks";
import { providerName } from "../../api/providerDetails";
import { BrandMark } from "../../components/Brand";
import { ErrorState, LoadingState } from "../../components/States";
import { useWorkspaceDraft } from "../../hooks/useWorkspaceDraft";
import { useWorkspace } from "../../providers";
import { SearchParameterSetup } from "../search-parameters/SearchParameterSetup";
import type { Draft } from "../search-parameters/draft";

type ProviderName = Provider["provider"];
type WorkspaceForm = {
  name: string;
  slug: string;
  root_path: string;
};

const steps = ["Workspace", "Search setup", "Posting links"];
const defaultProvider: ProviderName = "openai";

export function WelcomePage() {
  const navigate = useNavigate();
  const workspaces = useWorkspaces();
  const { workspaceId, setWorkspaceId } = useWorkspace();
  const workspace =
    workspaces.data?.find((item) => item.id === workspaceId) ?? workspaces.data?.[0];
  const activeWorkspaceId = workspace?.id ?? "";
  const parameters = useSearchParameters(activeWorkspaceId);
  const { edits: searchDraft } = useWorkspaceDraft<Draft>(
    activeWorkspaceId,
    "search",
    parameters.data?.version ?? 0,
  );
  const [completedWorkspaceId, setCompletedWorkspaceId] = useState<string | null>(null);

  if (workspaces.isLoading) return <LoadingState label="Reading setup progress" />;
  if (workspaces.isError) return <ErrorState error={workspaces.error} />;
  if (activeWorkspaceId && parameters.isLoading)
    return <LoadingState label="Reading search setup" />;
  if (activeWorkspaceId && parameters.isError && !parameters.data)
    return <ErrorState error={parameters.error} />;

  const unfinished = searchDraft !== null && completedWorkspaceId !== activeWorkspaceId;
  const currentStep = !workspace ? 0 : unfinished || (parameters.data?.version ?? 0) === 0 ? 1 : 2;

  return (
    <main className="welcome" id="main-content" tabIndex={-1}>
      <section className="welcome-card welcome-card--onboarding">
        <BrandMark size="large" />
        <p className="eyebrow">Private by default</p>
        <h1>Start your free job search.</h1>
        <p>
          Create a local workspace, save your search preferences, and add posting links. Free
          discovery needs no provider key or CV.
        </p>
        <ol aria-label="Onboarding progress" className="onboarding-steps">
          {steps.map((label, index) => (
            <li
              aria-current={index === currentStep ? "step" : undefined}
              data-state={
                index < currentStep ? "complete" : index === currentStep ? "current" : "upcoming"
              }
              key={label}
            >
              <span>{index + 1}</span>
              {label}
            </li>
          ))}
        </ol>
        {currentStep === 0 && <WorkspaceStep onCreated={setWorkspaceId} />}
        {currentStep === 1 && workspace && (
          <section aria-labelledby="onboarding-search-heading" className="onboarding-step">
            <h2 id="onboarding-search-heading">Basic search preferences</h2>
            <p>
              Save the places, work arrangements, pay, and role preferences you have supplied. You
              can return here or edit Search setup in Settings.
            </p>
            <div className="button-row">
              <Link className="button button--quiet" to="/settings?tab=search">
                Review search setup in Settings
              </Link>
            </div>
            <SearchParameterSetup
              mode="walkthrough"
              onSkip={() => {
                void navigate("/find?tab=pending");
              }}
              onSaved={() => setCompletedWorkspaceId(workspace.id)}
              workspaceId={workspace.id}
            />
          </section>
        )}
        {currentStep === 2 && workspace && <ReadyStep workspaceName={workspace.name} />}
        {workspace && (
          <section aria-labelledby="optional-paid-heading" className="onboarding-step">
            <h2 id="optional-paid-heading">Paid features (optional)</h2>
            <p>
              Search-vocabulary suggestions, guided search, evaluation, and drafting need a provider
              key and master CV. They are optional paid actions. Manual search setup, posting-link
              intake, board scans, tracking, and posting refresh need neither.
            </p>
            <div className="button-row">
              <Link className="button button--quiet" to="/settings?tab=provider">
                Configure provider access in Settings
              </Link>
              <Link className="button button--quiet" to="/cv">
                Import a master CV
              </Link>
            </div>
            <p>
              <a href="https://github.com/sethstraw/tailorcv/blob/main/docs/user-guide.md#choose-the-free-or-paid-path">
                When to use each free or paid route
              </a>
              {" | "}
              <a href="https://github.com/sethstraw/tailorcv/blob/main/docs/validation.md#known-limits-in-010">
                Current release limitations
              </a>
            </p>
          </section>
        )}
      </section>
    </main>
  );
}

function WorkspaceStep({ onCreated }: { onCreated: (id: string) => void }) {
  const providers = useProviders();
  const choices = providers.data?.length ? providers.data : [{ provider: defaultProvider }];
  const [provider, setProvider] = useState<ProviderName>(defaultProvider);
  const initialize = useInitializeWorkspace();
  const { register, handleSubmit, formState } = useForm<WorkspaceForm>({
    defaultValues: { name: "My Job Search", slug: "my-job-search", root_path: "" },
  });
  return (
    <section aria-labelledby="workspace-step-heading" className="onboarding-step">
      <h2 id="workspace-step-heading">Create the workspace</h2>
      <p>Your workspace keeps this search and its records together on your computer.</p>
      <form
        onSubmit={(event) =>
          void handleSubmit((values) =>
            initialize.mutate(
              {
                name: values.name,
                slug: values.slug,
                root_path: values.root_path || undefined,
                provider,
              },
              { onSuccess: (workspace) => onCreated(workspace.id) },
            ),
          )(event)
        }
      >
        <label>
          Workspace name
          <input
            {...register("name", { required: "Enter a workspace name." })}
            aria-describedby={formState.errors.name ? "workspace-name-error" : undefined}
            aria-invalid={Boolean(formState.errors.name)}
          />
        </label>
        <label>
          Short name
          <input
            {...register("slug", { required: true, pattern: /^[a-z0-9-]+$/ })}
            aria-describedby={formState.errors.slug ? "workspace-slug-error" : undefined}
            aria-invalid={Boolean(formState.errors.slug)}
          />
        </label>
        <details>
          <summary>Provider for paid features</summary>
          <p>
            This records a provider preference only. No key or provider call is needed to create the
            workspace. Add a key in Settings when you want paid features.
          </p>
          <label>
            Provider
            <select
              onChange={(event) => setProvider(event.target.value as ProviderName)}
              value={provider}
            >
              {choices.map((item) => (
                <option key={item.provider} value={item.provider}>
                  {providerName(item.provider)}
                </option>
              ))}
            </select>
          </label>
        </details>
        <details>
          <summary>Use a different workspace folder</summary>
          <label>
            Workspace folder <span>(optional)</span>
            <input {...register("root_path")} placeholder="Defaults to Documents/TailorCV" />
          </label>
        </details>
        {formState.errors.name && (
          <p className="field-error" id="workspace-name-error" role="alert">
            Enter a workspace name.
          </p>
        )}
        {formState.errors.slug && (
          <p className="field-error" id="workspace-slug-error" role="alert">
            Use lowercase letters, numbers, and hyphens.
          </p>
        )}
        {initialize.isError && <p className="field-error">{initialize.error.message}</p>}
        <button className="button button--primary" disabled={initialize.isPending} type="submit">
          {initialize.isPending ? "Creating" : "Create workspace"}
        </button>
      </form>
    </section>
  );
}

function ReadyStep({ workspaceName }: { workspaceName: string }) {
  return (
    <section aria-labelledby="ready-step-heading" className="onboarding-step onboarding-ready">
      <h2 id="ready-step-heading">{workspaceName}: search preferences saved</h2>
      <p>
        Add posting links to your queue. Review your search preferences and registered sources
        before starting a scan.
      </p>
      <div className="button-row">
        <Link className="button button--primary" to="/find?tab=pending">
          Find jobs
        </Link>
        <Link className="button button--quiet" to="/settings?tab=search">
          Review search setup in Settings
        </Link>
      </div>
    </section>
  );
}
