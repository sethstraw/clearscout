import { useState } from "react";

import { api, type Tag } from "../api/client";
import { useWorkspaceMutation } from "../api/hooks";
import { Icons } from "./Icons";
import { tagColorClass } from "./tagColor";

type TagEntityType = Tag["assignments"][number]["entity_type"];

const selectorLabels: Record<TagEntityType, string> = {
  application: "Tag this application",
  job_posting: "Tag this job",
};

function AssignedTags({
  onRemove,
  tags,
}: {
  onRemove: (tagId: string) => void;
  tags: readonly Tag[];
}) {
  return (
    <div>
      {tags.map((tag) => (
        <span key={tag.id}>
          <i className={tagColorClass(tag.color)} />
          {tag.name}
          <button
            aria-label={`Remove ${tag.name} tag`}
            onClick={() => onRemove(tag.id)}
            type="button"
          >
            <Icons.close height={12} width={12} />
          </button>
        </span>
      ))}
      {!tags.length && <small>No tags</small>}
    </div>
  );
}

export function EntityTags({
  entityId,
  entityType,
  tags,
  workspaceId,
}: {
  entityId: string;
  entityType: TagEntityType;
  tags: readonly Tag[];
  workspaceId: string;
}) {
  const [selectedTagId, setSelectedTagId] = useState("");
  const assign = useWorkspaceMutation(
    workspaceId,
    (tagId: string) =>
      api.assignTag(tagId, {
        workspace_id: workspaceId,
        entity_type: entityType,
        entity_id: entityId,
      }),
    { onSuccess: () => setSelectedTagId("") },
  );
  const unassign = useWorkspaceMutation(workspaceId, (tagId: string) =>
    api.unassignTag(tagId, workspaceId, entityType, entityId),
  );
  const assigned = tags.filter((tag) =>
    tag.assignments.some(
      (assignment) => assignment.entity_type === entityType && assignment.entity_id === entityId,
    ),
  );
  const available = tags.filter((tag) => !assigned.some((item) => item.id === tag.id));

  return (
    <aside className="job-tags">
      <AssignedTags onRemove={(tagId) => unassign.mutate(tagId)} tags={assigned} />
      <form
        onSubmit={(event) => {
          event.preventDefault();
          assign.mutate(selectedTagId);
        }}
      >
        <select
          aria-label={selectorLabels[entityType]}
          onChange={(event) => setSelectedTagId(event.target.value)}
          required
          value={selectedTagId}
        >
          <option value="">Choose tag</option>
          {available.map((tag) => (
            <option key={tag.id} value={tag.id}>
              {tag.name}
            </option>
          ))}
        </select>
        <button className="button button--quiet" type="submit">
          Add tag
        </button>
      </form>
    </aside>
  );
}
