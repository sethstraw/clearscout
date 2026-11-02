import { Icons } from "./Icons";
import type { SortDirection } from "./sorting";

type SortableHeaderProps<Key extends string> = {
  activeKey: Key;
  column: Key;
  description?: string;
  direction: SortDirection;
  label: string;
  onSort: (column: Key) => void;
};

export function SortableHeader<Key extends string>({
  activeKey,
  column,
  description,
  direction,
  label,
  onSort,
}: SortableHeaderProps<Key>) {
  const active = activeKey === column;
  const nextDirection = active && direction === "asc" ? "descending" : "ascending";
  const Indicator = active
    ? direction === "asc"
      ? Icons.sortAscending
      : Icons.sortDescending
    : Icons.sortUnsorted;
  return (
    <th
      aria-sort={active ? (direction === "asc" ? "ascending" : "descending") : "none"}
      scope="col"
    >
      <button
        aria-label={`Sort by ${label} ${nextDirection}`}
        className={`sort-button${active ? " sort-button--active" : ""}`}
        onClick={() => onSort(column)}
        title={description}
        type="button"
      >
        <span>{label}</span>
        <Indicator height={12} width={12} />
      </button>
    </th>
  );
}
