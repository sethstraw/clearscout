export const EMPLOYER_NOT_STATED = "Employer not stated";

/** The URL's host for display without a leading "www.", or "" when there is no usable URL. */
export function postingHost(url: string | null): string {
  if (!url) return "";
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/** Where an untitled posting lives; the host is a location, never an employer. */
export function titleFallback(url: string | null): string {
  const host = postingHost(url);
  return host ? `posting at ${host}` : "this posting";
}

/** A posting's accessible name; a missing employer is said, and the host named as its source. */
export function postingLabel(
  title: string | null | undefined,
  employer: string | null | undefined,
  url: string | null,
): string {
  const displayTitle = title || titleFallback(url);
  if (employer) return `${displayTitle} at ${employer}`;
  const host = postingHost(url);
  return title && host
    ? `${title}, employer not stated, listed on ${host}`
    : `${displayTitle}, employer not stated`;
}
