/// <reference types="node" />
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, it } from "vitest";

import { HELP } from "./content";

function headingSlugs(markdown: string): Set<string> {
  return new Set(
    markdown
      .split("\n")
      .filter((line) => /^#{1,6} /.test(line))
      .map((line) =>
        line
          .replace(/^#+ /, "")
          .trim()
          .toLowerCase()
          .replace(/[^\w\- ]/g, "")
          .replace(/ /g, "-"),
      ),
  );
}

it("keeps the Review facts that left the queue toolbar in Review help", () => {
  const body = HELP["find.review"].body;
  expect(body).toContain("Open the posting or Details to check uncertain facts yourself.");
  expect(body).toContain("pages can reveal more");
  expect(body).toMatch(/counts cover every content state .* only the selected state otherwise/);
});

it("points every help entry at a heading that exists in the user guide", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const guide = readFileSync(resolve(here, "../../../docs/user-guide.md"), "utf8");
  const slugs = headingSlugs(guide);
  const anchors = Object.values(HELP).flatMap((entry) =>
    "docsAnchor" in entry ? [entry.docsAnchor] : [],
  );
  expect(anchors.length).toBeGreaterThan(0);
  expect(anchors.filter((anchor) => !slugs.has(anchor))).toEqual([]);
});
