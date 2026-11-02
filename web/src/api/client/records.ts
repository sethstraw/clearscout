import { requestJson as request, requestVoid } from "../http";
import type { components } from "../schema";
import type { Note, Tag, TagAssignment, SavedView, SavedViewInput } from "./types";

export const recordsApi = {
  notes: (workspaceId: string, entityType?: string, entityId?: string, search = "") => {
    const query = new URLSearchParams({ workspace_id: workspaceId });
    if (entityType && entityId) {
      query.set("entity_type", entityType);
      query.set("entity_id", entityId);
    }
    if (search) query.set("search", search);
    return request<Note[]>(`/notes?${query}`);
  },
  createNote: (body: components["schemas"]["NoteBody"]) =>
    request<Note>("/notes", { method: "POST", body: JSON.stringify(body) }),
  updateNote: (
    noteId: string,
    body: { workspace_id: string; title: string; body: string; pinned: boolean },
  ) =>
    request<Note>(`/notes/${encodeURIComponent(noteId)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  archiveNote: (noteId: string, workspaceId: string) =>
    request<Note>(
      `/notes/${encodeURIComponent(noteId)}?workspace_id=${encodeURIComponent(workspaceId)}`,
      { method: "DELETE" },
    ),
  tags: (workspaceId: string) =>
    request<Tag[]>(`/tags?workspace_id=${encodeURIComponent(workspaceId)}`),
  createTag: (body: { workspace_id: string; name: string; color: string }) =>
    request<Tag>("/tags", { method: "POST", body: JSON.stringify(body) }),
  updateTag: (tagId: string, body: { workspace_id: string; name: string; color: string }) =>
    request<Tag>(`/tags/${encodeURIComponent(tagId)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  archiveTag: (tagId: string, workspaceId: string) =>
    request<Tag>(
      `/tags/${encodeURIComponent(tagId)}?workspace_id=${encodeURIComponent(workspaceId)}`,
      { method: "DELETE" },
    ),
  assignTag: (tagId: string, body: components["schemas"]["TagAssignmentBody"]) =>
    request<TagAssignment>(`/tags/${encodeURIComponent(tagId)}/assignments`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  unassignTag: (
    tagId: string,
    workspaceId: string,
    entityType: "job_posting" | "application",
    entityId: string,
  ) => {
    const query = new URLSearchParams({
      workspace_id: workspaceId,
      entity_type: entityType,
      entity_id: entityId,
    });
    return requestVoid(`/tags/${encodeURIComponent(tagId)}/assignments?${query}`, {
      method: "DELETE",
    });
  },
  savedViews: (workspaceId: string, resource = "") =>
    request<SavedView[]>(
      `/saved-views?workspace_id=${encodeURIComponent(workspaceId)}${resource ? `&resource=${encodeURIComponent(resource)}` : ""}`,
    ),
  saveView: (viewId: string | null, body: SavedViewInput) =>
    request<SavedView>(viewId ? `/saved-views/${encodeURIComponent(viewId)}` : "/saved-views", {
      method: viewId ? "PUT" : "POST",
      body: JSON.stringify(body),
    }),
  archiveView: (viewId: string, workspaceId: string) =>
    request<SavedView>(
      `/saved-views/${encodeURIComponent(viewId)}?workspace_id=${encodeURIComponent(workspaceId)}`,
      { method: "DELETE" },
    ),
};
