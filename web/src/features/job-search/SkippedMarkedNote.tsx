/** Names the favorite and kept rows a dismissal left in the queue. */
export function SkippedMarkedNote({ count }: { count: number }) {
  if (count === 0) return null;
  return (
    <p className="field-note" role="status">
      {count} favorite or kept {count === 1 ? "row" : "rows"} skipped.
    </p>
  );
}
