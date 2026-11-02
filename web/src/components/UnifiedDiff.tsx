import { useMemo } from "react";

type DiffKind = "add" | "del" | "hunk" | "file" | "context";

type DiffRow = {
  kind: DiffKind;
  text: string;
  // Header lines have no derived line number.
  oldNumber: number | null;
  newNumber: number | null;
};

function diffKind(line: string): DiffKind {
  // Classify unified headers before content lines; require the header's trailing space.
  if (/^(\+\+\+|---) /.test(line)) return "file";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "context";
}

function diffRows(text: string): DiffRow[] {
  if (!text.trim()) return [];
  let oldNumber = 0;
  let newNumber = 0;
  let numbering = false;
  return text
    .replace(/\n$/, "")
    .split("\n")
    .map((line) => {
      const kind = diffKind(line);
      if (kind === "hunk") {
        const match = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
        numbering = match !== null;
        oldNumber = Number(match?.[1] ?? 0);
        newNumber = Number(match?.[2] ?? 0);
        return { kind, text: line, oldNumber: null, newNumber: null };
      }
      // The no-newline marker is metadata, not file content.
      if (!numbering || kind === "file" || line.startsWith("\\")) {
        return { kind, text: line, oldNumber: null, newNumber: null };
      }
      const row = {
        kind,
        text: line,
        oldNumber: kind === "add" ? null : oldNumber,
        newNumber: kind === "del" ? null : newNumber,
      };
      if (kind !== "add") oldNumber += 1;
      if (kind !== "del") newNumber += 1;
      return row;
    });
}

export function UnifiedDiff({ diff, label }: { diff: string; label?: string }) {
  const rows = useMemo(() => diffRows(diff), [diff]);
  const added = rows.filter((row) => row.kind === "add").length;
  const removed = rows.filter((row) => row.kind === "del").length;
  if (!rows.length) {
    return (
      <div className="unified-diff">
        {label && <p className="diff-summary">{label}</p>}
        <p className="diff-empty">No changes. The two sides are identical.</p>
      </div>
    );
  }
  return (
    <div className="unified-diff">
      <p className="diff-summary">
        {label ? `${label} ` : ""}
        <span className="diff-count diff-count--add">
          {added} line{added === 1 ? "" : "s"} added
        </span>{" "}
        <span className="diff-count diff-count--del">
          {removed} line{removed === 1 ? "" : "s"} removed
        </span>
      </p>
      <div className="diff-lines">
        {rows.map((row, index) => (
          <div className={`diff-line diff-line--${row.kind}`} key={index}>
            {/* The sign remains in the line text for assistive technology. */}
            <span aria-hidden="true" className="diff-line__number">
              {row.oldNumber ?? ""}
            </span>
            <span aria-hidden="true" className="diff-line__number">
              {row.newNumber ?? ""}
            </span>
            <code className="diff-line__text">{row.text || " "}</code>
          </div>
        ))}
      </div>
    </div>
  );
}
