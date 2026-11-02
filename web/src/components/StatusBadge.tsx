/**
 * One value, one label, one tone. The status vocabulary is imported so this
 * file exports only a component.
 */
import { statusEntry } from "./statuses";
import type { StatusEntry, StatusValue } from "./statuses";

type StatusBadgeProps =
  | { value: StatusValue | null | undefined; entry?: never; title?: string }
  | { entry: StatusEntry; value?: never; title?: string };

// An entry is for a value whose tone depends on the record, not the value alone.
export function StatusBadge({ value, entry, title }: StatusBadgeProps) {
  if (entry) {
    return (
      <span className={`status status--tone-${entry.tone}`} title={title}>
        {entry.label}
      </span>
    );
  }
  const normalized = value ?? "unknown";
  const known = statusEntry(normalized);
  return (
    <span className={`status status--${normalized} status--tone-${known.tone}`} title={title}>
      {known.label}
    </span>
  );
}
