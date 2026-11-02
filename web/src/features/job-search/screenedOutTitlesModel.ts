import type { ScreenedOutTitleOptions } from "../../api/client";

const SCREENED_OUT_TITLES_PAGE_SIZE = 100;

/** The default view's options, shared so the summary reads the same cache entry. */
export function needsReviewOptions(): ScreenedOutTitleOptions {
  return {
    needsReview: true,
    search: "",
    source: "",
    offset: 0,
    limit: SCREENED_OUT_TITLES_PAGE_SIZE,
  };
}
