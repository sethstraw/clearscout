type HistoryPaginationProps = {
  count: number;
  label: string;
  offset: number;
  pageSize: number;
  onPageChange: (offset: number) => void;
};

export function HistoryPagination({
  count,
  label,
  offset,
  pageSize,
  onPageChange,
}: HistoryPaginationProps) {
  if (count <= pageSize) return null;
  const end = Math.min(offset + pageSize, count);
  return (
    <nav aria-label={`${label} pages`} className="pagination">
      <button
        className="button button--quiet"
        disabled={offset === 0}
        onClick={() => onPageChange(Math.max(0, offset - pageSize))}
        type="button"
      >
        Previous
      </button>
      <span>
        {offset + 1}-{end} of {count}
      </span>
      <button
        className="button button--quiet"
        disabled={end >= count}
        onClick={() => onPageChange(offset + pageSize)}
        type="button"
      >
        Next
      </button>
    </nav>
  );
}
