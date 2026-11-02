const DIFF_CONTEXT = 3;
const DIFF_CELL_CAP = 4_000_000;

type DiffOp = { sign: " " | "-" | "+"; text: string };

function blockOps(before: string[], after: string[]): DiffOp[] {
  if (!before.length || !after.length || before.length * after.length > DIFF_CELL_CAP) {
    return [
      ...before.map((text): DiffOp => ({ sign: "-", text })),
      ...after.map((text): DiffOp => ({ sign: "+", text })),
    ];
  }
  const cols = after.length + 1;
  const table = new Int32Array((before.length + 1) * cols);
  for (let i = before.length - 1; i >= 0; i -= 1) {
    for (let j = after.length - 1; j >= 0; j -= 1) {
      table[i * cols + j] =
        before[i] === after[j]
          ? (table[(i + 1) * cols + j + 1] ?? 0) + 1
          : Math.max(table[(i + 1) * cols + j] ?? 0, table[i * cols + j + 1] ?? 0);
    }
  }
  const ops: DiffOp[] = [];
  let i = 0;
  let j = 0;
  while (i < before.length && j < after.length) {
    if (before[i] === after[j]) {
      ops.push({ sign: " ", text: before[i] ?? "" });
      i += 1;
      j += 1;
    } else if ((table[(i + 1) * cols + j] ?? 0) >= (table[i * cols + j + 1] ?? 0)) {
      ops.push({ sign: "-", text: before[i] ?? "" });
      i += 1;
    } else {
      ops.push({ sign: "+", text: after[j] ?? "" });
      j += 1;
    }
  }
  for (; i < before.length; i += 1) ops.push({ sign: "-", text: before[i] ?? "" });
  for (; j < after.length; j += 1) ops.push({ sign: "+", text: after[j] ?? "" });
  return ops;
}

/**
 * A line-level unified diff computed in the browser, since the API diffs only
 * stored versions and a draft is unsaved. Matching prefix and suffix are
 * stripped first, which keeps a one-line edit off the quadratic path.
 */
export function unifiedDiff(before: string, after: string): string {
  if (before === after) return "";
  const left = before.split("\n");
  const right = after.split("\n");
  let head = 0;
  while (head < left.length && head < right.length && left[head] === right[head]) head += 1;
  let tail = 0;
  while (
    tail < left.length - head &&
    tail < right.length - head &&
    left[left.length - 1 - tail] === right[right.length - 1 - tail]
  ) {
    tail += 1;
  }
  const ops: DiffOp[] = [
    ...left.slice(0, head).map((text): DiffOp => ({ sign: " ", text })),
    ...blockOps(left.slice(head, left.length - tail), right.slice(head, right.length - tail)),
    ...left.slice(left.length - tail).map((text): DiffOp => ({ sign: " ", text })),
  ];

  const nearChange = ops.map((_, index) =>
    ops
      .slice(Math.max(0, index - DIFF_CONTEXT), index + DIFF_CONTEXT + 1)
      .some((op) => op.sign !== " "),
  );
  const lines: string[] = [];
  let oldLine = 1;
  let newLine = 1;
  let index = 0;
  while (index < ops.length) {
    const op = ops[index];
    if (!op) break;
    if (!nearChange[index]) {
      if (op.sign !== "+") oldLine += 1;
      if (op.sign !== "-") newLine += 1;
      index += 1;
      continue;
    }
    const oldStart = oldLine;
    const newStart = newLine;
    const body: string[] = [];
    let oldCount = 0;
    let newCount = 0;
    while (index < ops.length && nearChange[index]) {
      const hunkOp = ops[index];
      if (!hunkOp) break;
      body.push(`${hunkOp.sign}${hunkOp.text}`);
      if (hunkOp.sign !== "+") {
        oldLine += 1;
        oldCount += 1;
      }
      if (hunkOp.sign !== "-") {
        newLine += 1;
        newCount += 1;
      }
      index += 1;
    }
    lines.push(`@@ -${oldStart},${oldCount} +${newStart},${newCount} @@`, ...body);
  }
  return lines.join("\n");
}
