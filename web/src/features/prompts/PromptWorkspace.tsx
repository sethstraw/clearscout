import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../../api/client";
import type { PromptDetail, PromptState } from "../../api/client";
import { usePrompt, usePromptVersions, usePrompts, useWorkspaceMutation } from "../../api/hooks";
import { ConfirmedActionButton } from "../../components/ConfirmedActionButton";
import { MarkdownWorkspace } from "../../components/MarkdownWorkspace";
import { Page } from "../../components/Page";
import { ErrorState, LoadingState } from "../../components/States";
import { UnifiedDiff } from "../../components/UnifiedDiff";
import { invariantBlocks, preserveInvariantBlocks, type InvariantBlock } from "./promptInvariants";
import { useWorkspace } from "../../providers";

/** View and version the instructions sent to the model. */
export function PromptsPage() {
  const { workspaceId } = useWorkspace();
  const prompts = usePrompts(workspaceId);
  const [selected, setSelected] = useState("");
  if (prompts.isLoading) return <LoadingState label="Reading the prompt suite" />;
  if (prompts.isError) return <ErrorState error={prompts.error} />;
  const all = prompts.data ?? [];
  const key = selected || all[0]?.key || "";
  const edited = all.filter((item) => item.origin === "override").length;
  const collisions = all.filter((item) => item.upgrade === "collision");
  return (
    <Page
      eyebrow="Settings"
      title="Prompts"
      description="What the model is told, before it is told anything about you. Editing one changes every run after it and none before it."
    >
      <section className="panel">
        <header>
          <div>
            <h2>The suite</h2>
            <p>
              {all.length} prompts, {edited} edited. An unedited prompt has nothing stored, so it
              stays on the shipped text and picks up any change to it.
            </p>
          </div>
        </header>
        {collisions.length > 0 && (
          <div className="panel-body">
            <p className="field-note">
              {collisions.length} edited prompt{collisions.length === 1 ? " has" : "s have"} a
              shipped default that also changed. Your text is still what runs. Open it to compare.
            </p>
          </div>
        )}
        <ul className="prompt-picker">
          {stages(all).map(([stage, items]) => (
            <li key={stage}>
              <p className="prompt-picker__stage">{stage.replaceAll("_", " ")}</p>
              <ul>
                {items.map((item) => (
                  <li key={item.key}>
                    <button
                      aria-current={item.key === key}
                      className={
                        item.key === key ? "prompt-picker__item selected" : "prompt-picker__item"
                      }
                      onClick={() => setSelected(item.key)}
                      type="button"
                    >
                      <strong>{role(item.key)}</strong>
                      <span>{upgradeLabel(item)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      </section>
      {key && <PromptEditor key={key} promptKey={key} workspaceId={workspaceId} />}
    </Page>
  );
}

/** Split a prompt key into the stage it belongs to and the role it plays. */
function parts(promptKey: string): [string, string] {
  const cut = promptKey.lastIndexOf("_");
  const role = cut === -1 ? "" : promptKey.slice(cut + 1);
  return role === "system" || role === "user" ? [promptKey.slice(0, cut), role] : [promptKey, ""];
}

function role(promptKey: string): string {
  const [, name] = parts(promptKey);
  if (!name) return promptKey;
  return name === "system" ? "System" : "User";
}

/** Group the suite by stage, keeping the order the server sent. */
function stages(all: PromptState[]): [string, PromptState[]][] {
  const grouped = new Map<string, PromptState[]>();
  for (const item of all) {
    const [stage] = parts(item.key);
    grouped.set(stage, [...(grouped.get(stage) ?? []), item]);
  }
  return [...grouped];
}

function upgradeLabel(item: PromptState): string {
  if (item.origin === "packaged") return "As shipped";
  if (item.upgrade === "collision") return `Your v${item.version}, default moved too`;
  if (item.upgrade === "adopted") return "Took the newer default";
  return `Your version ${item.version}`;
}

function PromptEditor({ promptKey, workspaceId }: { promptKey: string; workspaceId: string }) {
  const detail = usePrompt(workspaceId, promptKey);
  const versions = usePromptVersions(workspaceId, promptKey);
  const [draft, setDraft] = useState<string | null>(null);
  const [compare, setCompare] = useState<number | null>(null);
  const save = useWorkspaceMutation(
    workspaceId,
    (content: string) =>
      api.savePrompt(promptKey, {
        workspace_id: workspaceId,
        content,
        expected_version: detail.data?.version,
      }),
    { successMessage: "Prompt saved as a new version." },
  );
  const revert = useWorkspaceMutation(
    workspaceId,
    (version: number) => api.revertPrompt(promptKey, { workspace_id: workspaceId, version }),
    { successMessage: "Prompt restored as a new version." },
  );
  const reset = useWorkspaceMutation(workspaceId, () => api.resetPrompt(promptKey, workspaceId), {
    successMessage: "Prompt returned to the shipped text.",
  });
  if (detail.isLoading) return <LoadingState label="Opening prompt" />;
  if (detail.isError) return <ErrorState error={detail.error} />;
  const data = detail.data;
  if (!data) return null;
  const content = draft ?? data.content;
  const dirty = draft !== null && draft !== data.content;
  const protectedBlocks = invariantBlocks(data.content);
  return (
    <article className="panel prompt-editor">
      <header>
        <div>
          <h2>{promptKey}</h2>
          <p>{describe(data)}</p>
        </div>
        <div className="button-row">
          {data.origin === "override" && (
            <ConfirmedActionButton
              className="button button--quiet"
              disabled={reset.isPending}
              onConfirm={() => {
                reset.mutate(undefined);
                setDraft(null);
              }}
              options={{
                title: "Go back to the shipped prompt?",
                description:
                  "Your versions stay readable in the history below, and this prompt returns to the text the application ships.",
                confirmLabel: "Use the shipped prompt",
              }}
            >
              Use the shipped prompt
            </ConfirmedActionButton>
          )}
          <button
            className="button button--primary"
            disabled={!dirty || save.isPending}
            onClick={() => save.mutate(content, { onSuccess: () => setDraft(null) })}
            type="button"
          >
            {save.isPending ? "Saving" : "Save as new version"}
          </button>
        </div>
      </header>
      <div className="panel-body">
        {save.isError && <p className="field-error">{save.error.message}</p>}
        {revert.isError && <p className="field-error">{revert.error.message}</p>}
        <VariableList data={data} />
        <ProtectedPromptRules blocks={protectedBlocks} />
        <MarkdownWorkspace
          content={content}
          onChange={(next) => setDraft(preserveInvariantBlocks(next, data.content))}
        />
        {dirty && (
          <p className="field-note">
            Unsaved. Saving appends a version; every run after it reads the new text and no earlier
            run changes.
          </p>
        )}
        {data.upgrade === "collision" && (
          <details className="prompt-collision">
            <summary>The shipped default changed too</summary>
            <p className="field-note">
              Your text is still what runs. This is the default the application now ships, kept here
              rather than written over yours. Use the shipped prompt above to take it.
            </p>
            <pre>{data.packaged_content}</pre>
          </details>
        )}
      </div>
      <VersionHistory
        compare={compare}
        onCompare={setCompare}
        onRevert={(version) => revert.mutate(version, { onSuccess: () => setDraft(null) })}
        promptKey={promptKey}
        versions={versions.data ?? []}
        workspaceId={workspaceId}
      />
    </article>
  );
}

function ProtectedPromptRules({ blocks }: { blocks: InvariantBlock[] }) {
  if (!blocks.length) return null;
  return (
    <details className="prompt-protection" open>
      <summary>Protected rules ({blocks.length})</summary>
      <p>
        These sections are carried into every override word for word. Edits inside the fenced
        regions are restored before the draft can be saved.
      </p>
      <ol>
        {blocks.map((block, index) => (
          <li key={`${block.start}-${index}`}>
            <pre>{block.body.trimEnd()}</pre>
          </li>
        ))}
      </ol>
    </details>
  );
}

function describe(data: PromptDetail): string {
  if (data.origin === "packaged")
    return "Running the text the application ships. Nothing is stored for it.";
  return `Running your version ${data.version}. The shipped text is kept for comparison.`;
}

/** What the program fills in, so an edit cannot rename a hole it does not own. */
function VariableList({ data }: { data: PromptDetail }) {
  if (!data.variables.length) return <p className="field-note">This prompt takes no values.</p>;
  return (
    <p className="field-note">
      Values this prompt must keep:{" "}
      {data.variables.map((item, index) => (
        <span key={item.name}>
          {index > 0 && ", "}
          <code>{`{{${item.name}}}`}</code>
        </span>
      ))}
      . Removing one, or inventing another, is refused when you save.
    </p>
  );
}

function VersionHistory({
  compare,
  onCompare,
  onRevert,
  promptKey,
  versions,
  workspaceId,
}: {
  compare: number | null;
  onCompare: (version: number | null) => void;
  onRevert: (version: number) => void;
  promptKey: string;
  versions: { id: string; version: number; created_at: string; restores_version: number | null }[];
  workspaceId: string;
}) {
  const diff = useQuery({
    queryKey: ["prompt-diff", workspaceId, promptKey, compare] as const,
    queryFn: ({ signal }) => api.promptDiff(promptKey, workspaceId, compare ?? 0, 0, signal),
    enabled: compare !== null,
  });
  if (!versions.length) {
    return (
      <div className="panel-body">
        <p className="field-note">No saved versions. This prompt has never been edited here.</p>
      </div>
    );
  }
  return (
    <div className="panel-body">
      <h3>Your versions</h3>
      <div className="table-wrap">
        <table aria-label="Prompt version history">
          <thead>
            <tr>
              <th scope="col">Version</th>
              <th scope="col">Saved</th>
              <th scope="col">Note</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {versions.map((item) => (
              <tr key={item.id}>
                <th scope="row">{item.version}</th>
                <td>{new Date(item.created_at).toLocaleString()}</td>
                <td>
                  {item.restores_version === null
                    ? "-"
                    : `Same text as version ${item.restores_version}`}
                </td>
                <td>
                  <div className="row-actions">
                    <button
                      className="button button--quiet"
                      onClick={() => onCompare(compare === item.version ? null : item.version)}
                      type="button"
                    >
                      {compare === item.version ? "Hide comparison" : "Compare with shipped"}
                    </button>
                    <ConfirmedActionButton
                      className="button button--quiet"
                      onConfirm={() => onRevert(item.version)}
                      options={{
                        title: `Go back to prompt version ${item.version}?`,
                        description:
                          "This appends a new version with the older text. The existing history remains unchanged.",
                        confirmLabel: "Append restored version",
                      }}
                    >
                      Restore as new version
                    </ConfirmedActionButton>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {compare !== null && diff.data && (
        <UnifiedDiff
          diff={diff.data.diff}
          label={`Version ${compare} against the shipped prompt.`}
        />
      )}
      <p className="field-note">
        Going back appends a version rather than deleting anything, so the history stays a record of
        what was actually run and when.
      </p>
    </div>
  );
}
