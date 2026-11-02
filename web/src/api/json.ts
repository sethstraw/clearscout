/** Render one untyped JSON value as text; a non-scalar falls back rather than printing "[object Object]". */
export function text(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}
