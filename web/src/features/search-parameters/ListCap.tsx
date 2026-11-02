export type MoveDirection = "up" | "down";

/** Tells the user ranking reads only the first rows of a list that guided search reads whole. */
export function UsedFirstNote({ cap, count, noun }: { cap: number; count: number; noun: string }) {
  if (count <= cap) return null;
  return (
    <p className="field-note list-cap-note" role="status">
      Ranking reads the first {cap} of {count} {noun}; the rest still shape guided searches. Move a
      row into the first {cap} to rank against it.
    </p>
  );
}

export function OrderControls({
  count,
  index,
  label,
  onMove,
  onRef,
}: {
  count: number;
  index: number;
  label: string;
  onMove: (from: number, to: number, direction: MoveDirection) => void;
  onRef: (direction: MoveDirection, element: HTMLButtonElement | null) => void;
}) {
  return (
    <div className="order-controls">
      <button
        className="button button--quiet"
        disabled={index === 0}
        onClick={() => onMove(index, index - 1, "up")}
        ref={(element) => {
          onRef("up", element);
        }}
        type="button"
      >
        Move up <span className="visually-hidden">{label}</span>
      </button>
      <button
        className="button button--quiet"
        disabled={index >= count - 1}
        onClick={() => onMove(index, index + 1, "down")}
        ref={(element) => {
          onRef("down", element);
        }}
        type="button"
      >
        Move down <span className="visually-hidden">{label}</span>
      </button>
    </div>
  );
}
