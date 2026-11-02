import { useState } from "react";

import type { ScreenedOutTitle } from "../../api/client";
import { useReviewScreenedOutTitles, useScreenedOutTitles } from "../../api/hooks";
import { safeHttpUrl } from "../../api/safeUrl";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { needsReviewOptions } from "./screenedOutTitlesModel";

type ReviewFilter = "needs_review" | "reviewed" | "all";

const REVIEW_FILTER: Record<ReviewFilter, boolean | null> = {
  needs_review: true,
  reviewed: false,
  all: null,
};

const STATE_LABEL: Record<ScreenedOutTitle["review_state"], string> = {
  new: "New",
  changed: "Finding changed",
  reviewed: "Reviewed",
};

function titleFinding(item: ScreenedOutTitle): string {
  if (!item.title_code) return "No title finding recorded";
  const code = item.title_code.replaceAll("_", " ");
  return item.title_subject ? `${code}: ${item.title_subject}` : code;
}

function day(value: string): string {
  return new Date(value).toLocaleDateString();
}

/** Review screened-out titles across every scan, one row per address. */
export function ScreenedOutTitles({ workspaceId }: { workspaceId: string }) {
  const [filter, setFilter] = useState<ReviewFilter>("needs_review");
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const debouncedSearch = useDebouncedValue(search, 250);
  const titles = useScreenedOutTitles(workspaceId, {
    ...needsReviewOptions(),
    needsReview: REVIEW_FILTER[filter],
    search: debouncedSearch,
    source,
    offset,
  });
  const review = useReviewScreenedOutTitles(workspaceId);
  const page = titles.data;
  const update = (change: () => void) => {
    change();
    setOffset(0);
    setSelected(new Set());
  };
  const markReviewed = async (ids: string[]) => {
    try {
      await review.mutateAsync(ids);
      setSelected(new Set());
    } catch {
      // The mutation error renders below.
    }
  };

  if (titles.isError) return <ErrorState error={titles.error} />;
  if (!page) return <LoadingState label="Reading screened-out titles" />;
  const reviewable = page.items.filter((item) => item.review_state !== "reviewed");
  const pageStart = page.total === 0 ? 0 : page.offset + 1;
  const pageEnd = Math.min(page.offset + page.limit, page.total);
  const changing = titles.isFetching || search !== debouncedSearch;
  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <section className="panel" aria-labelledby="screened-out-titles-heading">
      <header>
        <div>
          <h2 id="screened-out-titles-heading">Screened-out titles</h2>
          <p>
            Every listing a scan screened out, one row per address across all scans. A reviewed
            title comes back when a later scan reads a different title finding for it. A listing
            since added to pending leaves this list.
          </p>
          <p className="field-note">
            {page.needs_review_count} new or changed of {page.all_count} screened-out{" "}
            {page.all_count === 1 ? "title" : "titles"}.
          </p>
        </div>
      </header>
      <section className="toolbar" aria-label="Screened-out title filters">
        <label>
          <span>Review state</span>
          <select
            onChange={(event) => update(() => setFilter(event.target.value as ReviewFilter))}
            value={filter}
          >
            <option value="needs_review">New or changed</option>
            <option value="reviewed">Reviewed</option>
            <option value="all">All</option>
          </select>
        </label>
        <label className="search-field">
          <span>Search screened-out titles</span>
          <input
            onChange={(event) => update(() => setSearch(event.target.value))}
            placeholder="Employer, title, or source"
            type="search"
            value={search}
          />
        </label>
        <label>
          <span>Source</span>
          <select onChange={(event) => update(() => setSource(event.target.value))} value={source}>
            <option value="">All sources</option>
            {page.sources.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <button
          className="button"
          type="button"
          disabled={selected.size === 0 || review.isPending}
          onClick={() => void markReviewed([...selected])}
        >
          Mark {selected.size || ""} selected reviewed
        </button>
      </section>
      <p aria-live="polite" className="field-note">
        Showing {pageStart}-{pageEnd} of {page.total} {page.total === 1 ? "title" : "titles"}.
      </p>
      {review.isError ? <ErrorState error={review.error} /> : null}
      {page.items.length ? (
        <div className="table-wrap">
          <table aria-label="Screened-out titles" aria-busy={titles.isFetching}>
            <thead>
              <tr>
                <th scope="col">
                  <input
                    aria-label="Select every unreviewed title on this page"
                    checked={
                      reviewable.length > 0 && reviewable.every((item) => selected.has(item.id))
                    }
                    disabled={reviewable.length === 0}
                    onChange={(event) =>
                      setSelected(
                        event.target.checked
                          ? new Set(reviewable.map((item) => item.id))
                          : new Set(),
                      )
                    }
                    type="checkbox"
                  />
                </th>
                <th scope="col">Employer</th>
                <th scope="col">Title</th>
                <th scope="col">Title finding</th>
                <th scope="col">First seen</th>
                <th scope="col">Last seen</th>
                <th scope="col">State</th>
                <th scope="col">Source</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => {
                const url = safeHttpUrl(item.url);
                const label = `${item.title || "Untitled"} at ${item.employer || "an unstated employer"}`;
                return (
                  <tr key={item.id}>
                    <td>
                      {item.review_state !== "reviewed" && (
                        <input
                          aria-label={`Select ${label}`}
                          checked={selected.has(item.id)}
                          onChange={() => toggle(item.id)}
                          type="checkbox"
                        />
                      )}
                    </td>
                    <th scope="row">{item.employer || "Not stated"}</th>
                    <td>
                      {url ? (
                        <a href={url} rel="noreferrer" target="_blank">
                          {item.title || "View posting"}
                        </a>
                      ) : (
                        item.title || "Not stated"
                      )}
                    </td>
                    <td>{titleFinding(item)}</td>
                    <td>{day(item.first_seen_at)}</td>
                    <td>{day(item.last_seen_at)}</td>
                    <td>
                      {STATE_LABEL[item.review_state]}
                      {item.review_state !== "reviewed" && (
                        <button
                          aria-label={`Mark ${label} reviewed`}
                          className="button button--quiet"
                          disabled={review.isPending}
                          onClick={() => void markReviewed([item.id])}
                          type="button"
                        >
                          Mark reviewed
                        </button>
                      )}
                    </td>
                    <td>{item.source_name}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState>
          {filter === "needs_review" && !search && !source
            ? "Every screened-out title has been reviewed against its current title finding."
            : "No screened-out titles match these filters."}
        </EmptyState>
      )}
      {page.total > page.limit && (
        <nav className="pagination" aria-label="Screened-out title pages">
          <button
            className="button button--quiet"
            disabled={changing || page.offset === 0}
            onClick={() => {
              setOffset(Math.max(0, page.offset - page.limit));
              setSelected(new Set());
            }}
            type="button"
          >
            Previous
          </button>
          <button
            className="button button--quiet"
            disabled={changing || pageEnd >= page.total}
            onClick={() => {
              setOffset(page.offset + page.limit);
              setSelected(new Set());
            }}
            type="button"
          >
            Next
          </button>
        </nav>
      )}
    </section>
  );
}
