import { useState } from "react";

import { useJobLedger, useSavedViews, useSearchParameters } from "../../api/hooks";
import { viewFilters, viewSort, viewText, viewTextList, type JobSort } from "../../api/savedViews";
import { nextSort, type SortDirection } from "../../components/sorting";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";

export function useJobLedgerQueryState(workspaceId: string) {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [decision, setDecision] = useState("");
  const [completeness, setCompleteness] = useState("");
  const [place, setPlace] = useState("");
  const [employers, setEmployers] = useState<string[]>([]);
  const [postingStatus, setPostingStatus] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [sortKey, setSortKey] = useState<JobSort>("recorded_score");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const parameters = useSearchParameters(workspaceId);
  const places = (parameters.data?.parameters?.locations ?? []).map((location) => location.key);
  const debouncedSearch = useDebouncedValue(search, 250);
  const jobs = useJobLedger(workspaceId, {
    search: debouncedSearch,
    decision,
    status,
    completeness,
    place,
    employer: employers,
    postingStatus,
    includeArchived,
    sort: sortKey,
    descending: sortDirection === "desc",
  });
  const savedViews = useSavedViews(workspaceId, "jobs");
  const clearSelection = () => setSelected(new Set());
  const updateFilter = <Value>(setFilter: (value: Value) => void, value: Value) => {
    setFilter(value);
    clearSelection();
  };

  return {
    search,
    status,
    decision,
    completeness,
    place,
    employers,
    postingStatus,
    includeArchived,
    selected,
    sortKey,
    sortDirection,
    places,
    jobs,
    savedViews,
    clearSelection,
    updateSearch: (value: string) => updateFilter(setSearch, value),
    updateStatus: (value: string) => updateFilter(setStatus, value),
    updateDecision: (value: string) => updateFilter(setDecision, value),
    updateCompleteness: (value: string) => updateFilter(setCompleteness, value),
    updatePlace: (value: string) => updateFilter(setPlace, value),
    updateEmployers: (value: string[]) => updateFilter(setEmployers, value),
    updatePostingStatus: (value: string) => updateFilter(setPostingStatus, value),
    updateIncludeArchived: (value: boolean) => updateFilter(setIncludeArchived, value),
    applyView: (viewId: string) => {
      const view = savedViews.data?.find((item) => item.id === viewId);
      if (!view) return;
      const filters = viewFilters(view);
      setSearch(viewText(filters, "search"));
      setDecision(viewText(filters, "decision"));
      setStatus(viewText(filters, "status"));
      setCompleteness(viewText(filters, "completeness"));
      setPlace(viewText(filters, "place"));
      setEmployers(viewTextList(filters, "employer"));
      setPostingStatus(viewText(filters, "posting_status"));
      setIncludeArchived(filters.include_archived === true);
      const sort = viewSort(view.configuration.sort);
      setSortKey(sort?.key ?? "recorded_score");
      setSortDirection(sort && !sort.descending ? "asc" : "desc");
      clearSelection();
    },
    sortBy: (column: JobSort) => {
      const next = nextSort(sortKey, sortDirection, column);
      setSortKey(next.key);
      setSortDirection(next.direction);
      clearSelection();
    },
    setRowSelected: (jobId: string, checked: boolean) => {
      setSelected((current) => {
        const next = new Set(current);
        if (checked) next.add(jobId);
        else next.delete(jobId);
        return next;
      });
    },
    setVisibleSelected: (jobIds: string[], checked: boolean) =>
      setSelected(checked ? new Set(jobIds) : new Set()),
  };
}
