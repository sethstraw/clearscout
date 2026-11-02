import { fireEvent, render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import type { Guardrails } from "../../api/client";
import type { GuardrailPreviewReport, MasterCvContent } from "../../api/client/types";
import { AppProviders } from "../../providers";
import { server } from "../../test/msw/server";
import { GuardrailPreviewPanel } from "./GuardrailPreviewPanel";

const CV_CONTENT =
  "# Jane Q. Example\n\nAuthorized to work in Canada.\n\n---\n\n" +
  "## Professional Experience\n\n### Northwind Analytics\n\n" +
  "- Rebuilt the ingestion tier on Kafka, cutting latency across every feed.\n";

const RULES: Guardrails = {
  protected: ["Northwind Analytics"],
  required: ["Certified Kubernetes Administrator"],
  forbidden: ["Kafka"],
  work_authorization: null,
  distinctions: [],
  first_person: false,
  ascii_only: true,
};

const CURRENT_CV = {
  id: "cv-1",
  content_sha256: "digest",
  effective_at: "2026-08-20T00:00:00Z",
  is_current: true,
  byte_size: CV_CONTENT.length,
  content: CV_CONTENT,
  source_uri: "file:///synthetic/master-cv.md",
} satisfies MasterCvContent;

function install(preview: GuardrailPreviewReport) {
  server.use(
    http.post("*/api/v1/workspaces/:workspaceId/guardrails/preview", () =>
      HttpResponse.json(preview),
    ),
    http.get("*/api/v1/workspaces/:workspaceId/master-cv", () => HttpResponse.json(CURRENT_CV)),
  );
}

function renderPanel(rules: Guardrails = RULES) {
  render(
    <AppProviders>
      <GuardrailPreviewPanel rules={rules} workspaceId="workspace-preview" />
    </AppProviders>,
  );
}

afterEach(() => {
  window.localStorage.clear();
});

describe("GuardrailPreviewPanel", () => {
  it("shows per-rule matches from the current CV", async () => {
    install({
      forbidden: [
        {
          phrase: "Kafka",
          offsets: [CV_CONTENT.indexOf("Kafka")],
          matched: true,
          redraftable: true,
        },
      ],
      required: [
        {
          phrase: "Certified Kubernetes Administrator",
          offsets: [],
          matched: false,
          redraftable: false,
        },
      ],
      protected: [
        {
          phrase: "Northwind Analytics",
          offsets: [CV_CONTENT.indexOf("Northwind Analytics")],
          matched: true,
          redraftable: true,
        },
      ],
      work_authorization_precedence: {
        cv_line: "Authorized to work in Canada.",
        drafting_line: null,
        effective: "Authorized to work in Canada.",
        drops_cv_line: false,
      },
    });

    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Preview matches" }));

    expect(await screen.findByText("Kafka", { selector: "strong" })).toBeTruthy();
    expect(
      screen.getByText(
        "Already in your master CV. A draft that copies this text will fail the check.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("Certified Kubernetes Administrator")).toBeTruthy();
    expect(screen.getAllByText("Not found")).toHaveLength(1);
    expect(screen.getByText("Northwind Analytics", { selector: "strong" })).toBeTruthy();
    expect(screen.getByText(/No override; the CV line is used\./)).toBeTruthy();
    expect(screen.queryByRole("status", { name: /disagree/ })).toBeFalsy();
  });

  it("shows the effective work-authorization line when the CV and the drafting field differ", async () => {
    install({
      forbidden: [],
      required: [],
      protected: [],
      work_authorization_precedence: {
        cv_line: "Authorized to work in Canada.",
        drafting_line: "Authorized to work in the United States.",
        effective: "Authorized to work in the United States.",
        drops_cv_line: true,
      },
    });

    renderPanel({ ...RULES, work_authorization: "Authorized to work in the United States." });
    fireEvent.click(screen.getByRole("button", { name: "Preview matches" }));

    expect(await screen.findByText(/Drafting field will replace this CV line\./)).toBeTruthy();
    expect(
      screen.getByText(/The resume will read: "Authorized to work in the United States\."/),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Work-authorization wording disagrees between the CV and the drafting field",
      ),
    ).toBeTruthy();
  });
});
