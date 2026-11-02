import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBadge } from "./StatusBadge";
import type { StatusValue } from "./statuses";

describe("StatusBadge", () => {
  it("turns durable vocabulary into non-technical labels", () => {
    render(<StatusBadge value="package_ready" />);
    expect(screen.getByText("Package ready").className).toContain("status--package_ready");
  });

  it("labels an absent status without inventing a durable value", () => {
    render(<StatusBadge value={null} />);
    expect(screen.getByText("Unknown").className).toContain("status--tone-neutral");
  });

  it.each<[StatusValue, string]>([
    ["needs_review", "Needs review"],
    ["committing", "Committing"],
  ])("labels current durable state %s", (value, label) => {
    render(<StatusBadge value={value} />);
    expect(screen.getByText(label)).toBeTruthy();
  });

  it.each<[StatusValue, string, StatusValue, string]>([
    ["human", "Human", "model", "Model"],
    ["failed", "Failed", "cancelled", "Cancelled"],
    ["rejected", "Employer rejected", "withdrawn", "Withdrawn"],
  ])("keeps %s visually distinct from %s", (first, firstLabel, second, secondLabel) => {
    render(
      <>
        <StatusBadge value={first} />
        <StatusBadge value={second} />
      </>,
    );
    const tone = (label: string) => /status--tone-\w+/.exec(screen.getByText(label).className)?.[0];
    expect(tone(firstLabel)).toBeTruthy();
    expect(tone(secondLabel)).toBeTruthy();
    expect(tone(firstLabel)).not.toBe(tone(secondLabel));
  });

  it("labels posting status values that previously had no entry", () => {
    render(<StatusBadge value="active" />);
    expect(screen.getByText("Active").className).toContain("status--tone-positive");
  });

  it("distinguishes posting closure from an application that closed first", () => {
    render(
      <>
        <StatusBadge value="closed" />
        <StatusBadge value="closed_before_application" />
      </>,
    );
    expect(screen.getByText("Posting closed")).toBeTruthy();
    expect(screen.getByText("Closed before applying")).toBeTruthy();
  });
});
