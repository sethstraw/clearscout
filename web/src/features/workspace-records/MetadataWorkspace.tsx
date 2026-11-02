import { useState } from "react";

import { api } from "../../api/client";
import type { SavedView, Tag } from "../../api/client";
import { useSavedViews, useTags, useWorkspaceMutation } from "../../api/hooks";
import { ErrorState, LoadingState } from "../../components/States";
import { tagColorClass } from "../../components/tagColor";
import { ArchiveButton, EditableRecordRow, RecordSection } from "./RecordComponents";

export function MetadataWorkspace({ workspaceId }: { workspaceId: string }) {
  const tags = useTags(workspaceId);
  const views = useSavedViews(workspaceId);
  const [tagName, setTagName] = useState("");
  const [color, setColor] = useState("#4f46e5");
  const [viewName, setViewName] = useState("");
  const [filters, setFilters] = useState("{}");
  const [sort, setSort] = useState("[]");
  const saveTag = useWorkspaceMutation(
    workspaceId,
    () => api.createTag({ workspace_id: workspaceId, name: tagName, color }),
    { successMessage: "Tag created." },
  );
  const saveView = useWorkspaceMutation(
    workspaceId,
    () =>
      api.saveView(null, {
        workspace_id: workspaceId,
        resource: "jobs",
        name: viewName,
        configuration: viewConfiguration(filters, sort),
      }),
    { successMessage: "Saved view created." },
  );
  if (tags.isLoading || views.isLoading) {
    return <LoadingState label="Opening workspace metadata" />;
  }
  if (tags.isError || views.isError) {
    return <ErrorState error={tags.error ?? views.error} />;
  }
  return (
    <section className="detail-grid">
      <RecordSection
        empty="No tags yet."
        hasRecords={Boolean(tags.data?.length)}
        header={
          <>
            <h2>Tags</h2>
            <form
              className="inline-form"
              onSubmit={(event) => {
                event.preventDefault();
                saveTag.mutate(undefined);
              }}
            >
              <input
                aria-label="Tag name"
                onChange={(event) => setTagName(event.target.value)}
                placeholder="Priority"
                required
                value={tagName}
              />
              <label className="swatch-field">
                <span className="visually-hidden">Tag colour</span>
                <input
                  aria-label="Tag colour"
                  onChange={(event) => setColor(event.target.value)}
                  type="color"
                  value={color}
                />
              </label>
              <button className="button button--primary" type="submit">
                Create tag
              </button>
            </form>
          </>
        }
        listClassName="tag-list"
      >
        {tags.data?.map((tag) => (
          <TagRow key={tag.id} tag={tag} workspaceId={workspaceId} />
        ))}
      </RecordSection>
      <RecordSection
        empty="No saved views yet."
        hasRecords={Boolean(views.data?.length)}
        header={
          <>
            <h2>Saved job views</h2>
            <p className="field-note">A view stores filters and a sort order for evaluated jobs.</p>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                saveView.mutate(undefined);
              }}
            >
              <label>
                Name
                <input
                  onChange={(event) => setViewName(event.target.value)}
                  placeholder="For example: Remote, high fit"
                  required
                  value={viewName}
                />
              </label>
              <details className="advanced-view">
                <summary>Write the filters by hand</summary>
                <p className="field-note">
                  Only useful if you know the stored shape. Leave these alone to save a view holding
                  nothing but its name.
                </p>
                <label>
                  Filters (JSON)
                  <textarea
                    onChange={(event) => setFilters(event.target.value)}
                    rows={3}
                    value={filters}
                  />
                </label>
                <label>
                  Sort (JSON)
                  <textarea
                    onChange={(event) => setSort(event.target.value)}
                    rows={3}
                    value={sort}
                  />
                </label>
              </details>
              <button className="button button--primary" type="submit">
                Save view
              </button>
            </form>
          </>
        }
        listClassName="record-list"
      >
        {views.data?.map((view) => (
          <ViewRow key={view.id} view={view} workspaceId={workspaceId} />
        ))}
      </RecordSection>
    </section>
  );
}

function TagRow({ tag, workspaceId }: { tag: Tag; workspaceId: string }) {
  const [name, setName] = useState(tag.name);
  const [color, setColor] = useState(tag.color);
  const save = useWorkspaceMutation(
    workspaceId,
    () => api.updateTag(tag.id, { workspace_id: workspaceId, name, color }),
    { successMessage: "Tag changes saved." },
  );
  const archive = useWorkspaceMutation(workspaceId, () => api.archiveTag(tag.id, workspaceId), {
    successMessage: "Tag archived.",
  });
  return (
    <EditableRecordRow
      editLabel="Edit"
      editor={
        <form
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(undefined);
          }}
        >
          <input
            aria-label="Tag name"
            onChange={(event) => setName(event.target.value)}
            required
            value={name}
          />
          <input
            aria-label="Tag color"
            onChange={(event) => setColor(event.target.value)}
            type="color"
            value={color}
          />
          <div className="button-row">
            <button type="submit">Save</button>
            <ArchiveButton label="tag" onArchive={() => archive.mutate(undefined)} />
          </div>
        </form>
      }
    >
      <span className={tagColorClass(tag.color)} />
      {tag.name}
      <small>{tag.assignments.length} assignments</small>
    </EditableRecordRow>
  );
}

function ViewRow({ view, workspaceId }: { view: SavedView; workspaceId: string }) {
  const [name, setName] = useState(view.name);
  const [filters, setFilters] = useState(JSON.stringify(view.configuration.filters, null, 2));
  const [sort, setSort] = useState(JSON.stringify(view.configuration.sort, null, 2));
  const save = useWorkspaceMutation(
    workspaceId,
    () =>
      api.saveView(view.id, {
        workspace_id: workspaceId,
        resource: "jobs",
        name,
        configuration: viewConfiguration(filters, sort),
      }),
    { successMessage: "Saved view changes saved." },
  );
  const archive = useWorkspaceMutation(workspaceId, () => api.archiveView(view.id, workspaceId), {
    successMessage: "Saved view archived.",
  });
  return (
    <EditableRecordRow
      editLabel="View details"
      editor={
        <form
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(undefined);
          }}
        >
          <label>
            Name
            <input onChange={(event) => setName(event.target.value)} required value={name} />
          </label>
          <label>
            Filters (JSON)
            <textarea
              onChange={(event) => setFilters(event.target.value)}
              rows={3}
              value={filters}
            />
          </label>
          <label>
            Sort (JSON)
            <textarea onChange={(event) => setSort(event.target.value)} rows={3} value={sort} />
          </label>
          <div className="button-row">
            <button className="button button--primary" type="submit">
              Save changes
            </button>
            <ArchiveButton
              className="button button--quiet"
              label="saved view"
              onArchive={() => archive.mutate(undefined)}
            />
          </div>
        </form>
      }
    >
      <strong>{view.name}</strong>
      <span>Jobs</span>
    </EditableRecordRow>
  );
}

function viewConfiguration(filters: string, sort: string) {
  return {
    filters: JSON.parse(filters) as Record<string, unknown> | unknown[],
    sort: JSON.parse(sort) as Record<string, unknown> | unknown[],
  };
}
