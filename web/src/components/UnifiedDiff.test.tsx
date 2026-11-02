import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UnifiedDiff } from "./UnifiedDiff";

const DIFF =
  "--- v1\n+++ v2\n@@ -1,3 +1,4 @@\n # Summary\n-Old line\n+New line\n+Another new line\n";

function rowText(line: string) {
  return screen.getByText(line).parentElement?.textContent;
}

describe("UnifiedDiff", () => {
  it("counts the added and removed lines without counting the headers", () => {
    render(<UnifiedDiff diff={DIFF} />);

    expect(screen.getByText("2 lines added")).toBeTruthy();
    expect(screen.getByText("1 line removed")).toBeTruthy();
  });

  it("reads a deleted markdown rule as a removed line, not as a file header", () => {
    render(<UnifiedDiff diff={"--- v1\n+++ v2\n@@ -1,2 +1,1 @@\n Kept\n----\n"} />);

    expect(screen.getByText("0 lines added")).toBeTruthy();
    expect(screen.getByText("1 line removed")).toBeTruthy();
    expect(screen.getByText("----")).toBeTruthy();
  });

  it("numbers lines from the hunk header, old and new side separately", () => {
    render(
      <UnifiedDiff diff={"--- v1\n+++ v2\n@@ -12,3 +40,4 @@\n kept\n-gone\n+new\n kept two\n"} />,
    );

    expect(rowText("kept")).toBe("1240 kept");
    expect(rowText("-gone")).toBe("13-gone");
    expect(rowText("+new")).toBe("41+new");
    expect(rowText("kept two")).toBe("1442 kept two");
  });

  it("leaves the numbers blank on lines they cannot be derived for", () => {
    render(<UnifiedDiff diff={"--- v1\n+++ v2\n@@ -12,1 +40,1 @@\n kept\n"} />);

    expect(rowText("--- v1")).toBe("--- v1");
    expect(rowText("@@ -12,1 +40,1 @@")).toBe("@@ -12,1 +40,1 @@");
    expect(rowText("kept")).toBe("1240 kept");
  });

  it("numbers nothing when no hunk header says where the lines start", () => {
    render(<UnifiedDiff diff="+orphan" />);

    expect(rowText("+orphan")).toBe("+orphan");
  });

  it("states that an empty diff means no changes", () => {
    render(<UnifiedDiff diff="" label="Changes from v1 to this version." />);

    expect(screen.getByText("No changes. The two sides are identical.")).toBeTruthy();
    expect(screen.getByText("Changes from v1 to this version.")).toBeTruthy();
  });
});
