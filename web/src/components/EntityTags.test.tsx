import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw/http";
import { describe, expect, it } from "vitest";

import type { Tag, TagAssignment } from "../api/client/types";
import { AppProviders } from "../providers";
import { server } from "../test/msw/server";
import { EntityTags } from "./EntityTags";

const ASSIGNED = {
  id: "assignment-priority",
  tag_id: "tag-priority",
  tag_name: "Priority",
  color: "#123456",
  entity_type: "application",
  entity_id: "application-1",
} satisfies TagAssignment;

describe("EntityTags", () => {
  it("filters exact assignments and sends assign and unassign mutations for the entity", async () => {
    const assigned: string[] = [];
    const unassigned: string[] = [];
    server.use(
      http.post("*/api/v1/tags/:tagId/assignments", async ({ request }) => {
        assigned.push(`${new URL(request.url).pathname} ${await request.text()}`);
        return HttpResponse.json(ASSIGNED);
      }),
      http.delete("*/api/v1/tags/:tagId/assignments", ({ request }) => {
        const url = new URL(request.url);
        unassigned.push(`${url.pathname}${url.search}`);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const tags: Tag[] = [
      {
        id: "tag-priority",
        workspace_id: "workspace-tags",
        name: "Priority",
        color: "#123456",
        created_at: "2026-08-28T12:00:00Z",
        assignments: [ASSIGNED],
      },
      {
        id: "tag-follow-up",
        workspace_id: "workspace-tags",
        name: "Follow up",
        color: "#654321",
        created_at: "2026-08-28T12:00:00Z",
        assignments: [
          {
            id: "assignment-other",
            tag_id: "tag-follow-up",
            tag_name: "Follow up",
            color: "#654321",
            entity_type: "job_posting",
            entity_id: "job-1",
          },
        ],
      },
    ];

    render(
      <AppProviders>
        <EntityTags
          entityId="application-1"
          entityType="application"
          tags={tags}
          workspaceId="workspace-tags"
        />
      </AppProviders>,
    );

    const selector = screen.getByRole("combobox", { name: "Tag this application" });
    expect(screen.getByText("Priority")).toBeTruthy();
    expect(screen.queryByRole("option", { name: "Priority" })).toBeNull();
    expect(screen.getByRole("option", { name: "Follow up" })).toBeTruthy();

    fireEvent.change(selector, { target: { value: "tag-follow-up" } });
    fireEvent.click(screen.getByRole("button", { name: "Add tag" }));

    await waitFor(() =>
      expect(assigned).toEqual([
        `/api/v1/tags/tag-follow-up/assignments ${JSON.stringify({
          workspace_id: "workspace-tags",
          entity_type: "application",
          entity_id: "application-1",
        })}`,
      ]),
    );

    fireEvent.click(screen.getByRole("button", { name: "Remove Priority tag" }));

    await waitFor(() =>
      expect(unassigned).toEqual([
        "/api/v1/tags/tag-priority/assignments?workspace_id=workspace-tags&entity_type=application&entity_id=application-1",
      ]),
    );
  });
});
