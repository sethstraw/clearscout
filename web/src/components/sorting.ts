export type SortDirection = "asc" | "desc";

export function nextSort<Key extends string>(
  currentKey: Key,
  currentDirection: SortDirection,
  nextKey: Key,
): { key: Key; direction: SortDirection } {
  return {
    key: nextKey,
    direction: currentKey === nextKey && currentDirection === "asc" ? "desc" : "asc",
  };
}

export function compareValues(
  left: number | string | null | undefined,
  right: number | string | null | undefined,
  direction: SortDirection,
) {
  if (left == null && right == null) return 0;
  if (left == null) return 1;
  if (right == null) return -1;
  const result =
    typeof left === "number" && typeof right === "number"
      ? left - right
      : String(left).localeCompare(String(right), undefined, {
          numeric: true,
          sensitivity: "base",
        });
  return direction === "asc" ? result : -result;
}
