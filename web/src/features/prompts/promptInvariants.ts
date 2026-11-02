const OPEN = "<!--invariant-->";
const CLOSE = "<!--/invariant-->";

// The server owns this rule: a marker counts only at the start of a line, the
// opener is followed by a newline, and the body is taken verbatim.
const REGION = new RegExp(`^${escape(OPEN)}\\n([\\s\\S]*?)^${escape(CLOSE)}$`, "gm");

function escape(marker: string): string {
  return marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export type InvariantBlock = {
  body: string;
  raw: string;
  start: number;
  end: number;
};

export function invariantBlocks(content: string): InvariantBlock[] {
  const found: InvariantBlock[] = [];
  const pattern = new RegExp(REGION.source, REGION.flags);
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(content)) !== null) {
    found.push({
      body: match[1] ?? "",
      raw: match[0],
      start: match.index,
      end: match.index + match[0].length,
    });
  }
  return found;
}

export function preserveInvariantBlocks(next: string, baseline: string): string {
  const expected = invariantBlocks(baseline);
  if (!expected.length) return next;

  let result = next;
  for (const [index, block] of expected.entries()) {
    const current = invariantBlocks(result)[index];
    if (!current) return baseline;
    if (current.raw !== block.raw) {
      result = `${result.slice(0, current.start)}${block.raw}${result.slice(current.end)}`;
    }
  }
  return invariantBlocks(result).length === expected.length ? result : baseline;
}
