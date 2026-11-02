import { useRef, useState } from "react";

import { useAddFoundPosting, useCheckFoundPosting } from "../../api/hooks";
import { ApiError, type FoundPostingCheckResponse } from "../../api/client";
import { usePostingRefresh } from "./usePostingRefresh";

function verifiedPostingFields(result: FoundPostingCheckResponse): string[] {
  const facts = result.staged_facts;
  return [
    result.title ? "title" : "",
    result.employer ? "employer" : "",
    result.location || facts?.locations?.values?.length ? "location" : "",
    facts?.arrangements?.values?.length ? "work arrangement" : "",
    facts?.employment_types?.values?.length ? "employment type" : "",
  ].filter(Boolean);
}

function PostingCheckResult({ result }: { result: FoundPostingCheckResponse }) {
  if (result.status !== "readable") {
    return (
      <div className="posting-check posting-check--warning" role="alert">
        <strong>Publisher details could not be verified.</strong>
        <p>{result.note}</p>
        <p>Use a current publisher link, then check it again before adding.</p>
      </div>
    );
  }
  const locations = result.staged_facts?.locations?.values ?? [];
  const unresolvedLocations = result.staged_facts?.locations?.unresolved_values ?? [];
  const arrangements = result.staged_facts?.arrangements?.values ?? [];
  const employmentTypes = result.staged_facts?.employment_types?.values ?? [];
  return (
    <div className="posting-check" role="status">
      <strong>Publisher details found</strong>
      <dl>
        {result.title && (
          <>
            <dt>Title</dt>
            <dd>{result.title}</dd>
          </>
        )}
        {result.employer && (
          <>
            <dt>Employer</dt>
            <dd>{result.employer}</dd>
          </>
        )}
        {(locations.length > 0 || unresolvedLocations.length > 0 || result.location) && (
          <>
            <dt>Locations</dt>
            <dd>
              {[...locations, ...unresolvedLocations].join(", ") || result.location}
              {unresolvedLocations.length > 0 ? " (publisher summary unresolved)" : ""}
            </dd>
          </>
        )}
        {arrangements.length > 0 && (
          <>
            <dt>Work arrangement</dt>
            <dd>{arrangements.join(", ")}</dd>
          </>
        )}
        {employmentTypes.length > 0 && (
          <>
            <dt>Employment type</dt>
            <dd>{employmentTypes.join(", ")}</dd>
          </>
        )}
      </dl>
      <p>{result.note}</p>
    </div>
  );
}

type IntakeResult = {
  id: number;
  url: string;
  outcome: "adding" | "added" | "duplicate" | "invalid" | "failed";
  detail: string;
  read: string;
};

export function PendingDiscoveryIntake({
  onAdded,
  workspaceId,
}: {
  onAdded: (pendingDiscoveryId: string) => void;
  workspaceId: string;
}) {
  const addFound = useAddFoundPosting(workspaceId);
  const checkFound = useCheckFoundPosting(workspaceId);
  const refresh = usePostingRefresh(workspaceId);
  const [foundUrl, setFoundUrl] = useState("");
  const [foundTitle, setFoundTitle] = useState("");
  const [checkedUrl, setCheckedUrl] = useState("");
  const [foundCheck, setFoundCheck] = useState<FoundPostingCheckResponse | null>(null);
  const [results, setResults] = useState<IntakeResult[]>([]);
  const [summary, setSummary] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const foundUrlRef = useRef("");
  const nextResultId = useRef(0);
  const stagedUrls = useRef(new Set<string>());
  const links = foundUrl
    .split(/\r?\n/)
    .map((url) => url.trim())
    .filter(Boolean);
  const single = links.length === 1;

  const updateResult = (id: number, update: Partial<IntakeResult>) => {
    setResults((current) =>
      current.map((result) => (result.id === id ? { ...result, ...update } : result)),
    );
  };

  const addLinks = async () => {
    if (submitting || checkFound.isPending || !links.length) return;
    setSubmitting(true);
    setSummary("");
    const submitted = links.map((url) => ({
      id: nextResultId.current++,
      url,
      outcome: "adding" as const,
      detail: "Adding link...",
      read: "",
    }));
    setResults((current) => [...current, ...submitted]);
    const seen = new Set(stagedUrls.current);
    const reads: { resultId: number; pendingId: string }[] = [];
    const retryUrls: string[] = [];
    const counts = { added: 0, duplicate: 0, invalid: 0, failed: 0 };

    for (const item of submitted) {
      if (seen.has(item.url)) {
        counts.duplicate++;
        updateResult(item.id, {
          outcome: "duplicate",
          detail: "This link was already submitted. No second read was queued.",
        });
        continue;
      }
      seen.add(item.url);
      try {
        const url = new URL(item.url);
        if (url.protocol !== "http:" && url.protocol !== "https:")
          throw new Error("Unsupported URL protocol.");
      } catch {
        counts.invalid++;
        retryUrls.push(item.url);
        updateResult(item.id, {
          outcome: "invalid",
          detail: "Use a complete http or https posting link.",
        });
        continue;
      }
      try {
        const response = await addFound.mutateAsync({
          url: item.url,
          title: single ? foundTitle.trim() : "",
          employer: "",
          location: "",
          check_publisher_facts:
            single && checkedUrl === item.url && foundCheck?.status === "readable",
        });
        stagedUrls.current.add(item.url);
        counts.added++;
        const stored = response.pending_discovery;
        const subject =
          stored.title || (single ? foundTitle.trim() : "") || `posting from ${item.url}`;
        const identity = stored.employer ? `${subject} at ${stored.employer}` : subject;
        const action = response.outcome === "created" ? "Staged" : "Returned";
        const verification = response.posting_check;
        const verified = verification ? verifiedPostingFields(verification) : [];
        updateResult(item.id, {
          outcome: "added",
          detail:
            verification?.status === "readable"
              ? `${action} ${identity}. Publisher verified ${verified.join(", ") || "the posting response"}.`
              : `${action} ${identity} with unverified hints.`,
          read: "Saved; preparing its free posting read.",
        });
        reads.push({ resultId: item.id, pendingId: stored.id });
        onAdded(stored.id);
      } catch (error) {
        const detail = error instanceof Error ? error.message : "The posting could not be added.";
        const outcome =
          error instanceof ApiError && error.status === 409
            ? "duplicate"
            : error instanceof ApiError && error.status === 422
              ? "invalid"
              : "failed";
        counts[outcome]++;
        if (outcome !== "duplicate") retryUrls.push(item.url);
        updateResult(item.id, { outcome, detail });
      }
    }

    setFoundUrl(retryUrls.join("\n"));
    foundUrlRef.current = retryUrls.join("\n");
    if (!retryUrls.length) setFoundTitle("");
    setCheckedUrl("");
    setFoundCheck(null);
    checkFound.reset();

    for (let offset = 0; offset < reads.length; offset += 250) {
      const batch = reads.slice(offset, offset + 250);
      try {
        await refresh.refresh(batch.map((item) => item.pendingId));
        for (const item of batch)
          updateResult(item.resultId, {
            read: "Free posting read queued. Check the pending queue for the result.",
          });
      } catch (error) {
        const detail = error instanceof Error ? error.message : "The read queue is unavailable.";
        for (const item of batch)
          updateResult(item.resultId, {
            read: `Saved, but its read could not be queued: ${detail} Refresh this posting from the pending queue to retry.`,
          });
      }
    }
    setSummary(
      `Processed ${submitted.length} ${submitted.length === 1 ? "link" : "links"}: ${counts.added} added, ${counts.duplicate} ${counts.duplicate === 1 ? "duplicate" : "duplicates"}, ${counts.invalid} invalid or unsupported, ${counts.failed} failed.`,
    );
    setSubmitting(false);
  };

  return (
    <details className="menu-disclosure">
      <summary className="button button--quiet">Add a posting</summary>
      <div className="menu-disclosure__panel">
        <p>
          Paste direct public posting links, one per line. Each added posting gets a free
          policy-gated content read before it can enter the ready queue. Results stay below while
          you add more links.
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void addLinks();
          }}
        >
          <div className="source-field">
            <label htmlFor="found-posting-url">Posting link</label>
            <div className="source-field__control">
              <textarea
                id="found-posting-url"
                value={foundUrl}
                rows={4}
                disabled={submitting}
                aria-describedby="found-posting-help"
                onChange={(event) => {
                  setFoundUrl(event.target.value);
                  foundUrlRef.current = event.target.value.trim();
                  setCheckedUrl("");
                  setFoundCheck(null);
                  checkFound.reset();
                }}
                placeholder="https://jobs.example.com/postings/1234"
              />
            </div>
            <p className="field-note" id="found-posting-help">
              One link per line. Failed and invalid links stay here so you can correct or retry
              them.
            </p>
          </div>
          <div className="source-field">
            <label htmlFor="found-posting-title">Title, if you know it</label>
            <div className="source-field__control">
              <input
                id="found-posting-title"
                type="text"
                value={foundTitle}
                disabled={submitting || !single}
                onChange={(event) => setFoundTitle(event.target.value)}
              />
            </div>
            <p className="field-note">
              The optional title and publisher check apply to a single link.
            </p>
          </div>
          {foundCheck && <PostingCheckResult result={foundCheck} />}
          {checkFound.isError && (
            <div className="posting-check posting-check--warning" role="alert">
              <strong>The check request failed.</strong>
              <p>{checkFound.error.message}</p>
              <p>You can still add the link with your title as an unverified hint.</p>
            </div>
          )}
          <div className="button-row">
            <button
              className="button button--quiet"
              type="button"
              disabled={!single || checkFound.isPending || submitting}
              onClick={() => {
                const url = links[0];
                if (!single || !url) return;
                setCheckedUrl(url);
                setFoundCheck(null);
                checkFound.mutate(
                  { url },
                  {
                    onSuccess: (result) => {
                      if (foundUrlRef.current === url) setFoundCheck(result);
                    },
                  },
                );
              }}
            >
              {checkFound.isPending ? "Checking..." : "Check posting"}
            </button>
            <button
              className="button button--primary"
              type="submit"
              disabled={!links.length || checkFound.isPending || submitting}
            >
              {submitting
                ? "Adding links..."
                : links.length > 1
                  ? `Add ${links.length} postings`
                  : "Add posting"}
            </button>
          </div>
        </form>
        <p aria-live="polite" className="field-note" role="status">
          {summary}
        </p>
        {results.length > 0 && (
          <ol aria-label="Posting link results">
            {results.map((result) => (
              <li className="posting-check" key={result.id}>
                <p className="pending-origin__host">{result.url}</p>
                <strong>
                  {result.outcome === "adding"
                    ? "Adding"
                    : result.outcome === "added"
                      ? "Added"
                      : result.outcome === "duplicate"
                        ? "Duplicate"
                        : result.outcome === "invalid"
                          ? "Invalid or unsupported"
                          : "Failed"}
                </strong>
                <p>{result.detail}</p>
                {result.read && <p>{result.read}</p>}
              </li>
            ))}
          </ol>
        )}
      </div>
    </details>
  );
}
