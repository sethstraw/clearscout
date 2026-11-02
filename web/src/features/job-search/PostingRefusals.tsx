import { usePostingRefusals } from "../../api/hooks";
import { safeHttpUrl } from "../../api/safeUrl";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";

/** List retained posting refusals with the search settings that produced them. */
export function PostingRefusals({ workspaceId }: { workspaceId: string }) {
  const refusals = usePostingRefusals(workspaceId);
  if (refusals.isLoading) return <LoadingState label="Reading refused postings" />;
  if (refusals.isError) return <ErrorState error={refusals.error} />;
  return (
    <section className="panel" aria-labelledby="posting-refusals-heading">
      <header>
        <div>
          <h2 id="posting-refusals-heading">Refusal history</h2>
          <p>
            These are past refusal decisions, not the posting's current status. Each row names the
            saved search settings used for that decision when the version is known.
          </p>
        </div>
      </header>
      {refusals.data?.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Posting</th>
                <th scope="col">Condition</th>
                <th scope="col">Reason</th>
                <th scope="col">Search settings</th>
                <th scope="col">Refused</th>
              </tr>
            </thead>
            <tbody>
              {refusals.data.map((refusal) => {
                const url = safeHttpUrl(refusal.posting_url ?? "");
                return (
                  <tr key={refusal.id}>
                    <th scope="row">
                      {url ? (
                        <a href={url} rel="noreferrer" target="_blank">
                          {refusal.title} at {refusal.employer}
                        </a>
                      ) : (
                        <>
                          {refusal.title} at {refusal.employer}
                        </>
                      )}
                    </th>
                    <td>{refusal.condition}</td>
                    <td>{refusal.reason}</td>
                    <td>
                      {refusal.search_parameter_version === null
                        ? "No saved version"
                        : `v${refusal.search_parameter_version}`}
                    </td>
                    <td>{new Date(refusal.refused_at).toLocaleDateString()}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState>No posting has been refused before evaluation.</EmptyState>
      )}
    </section>
  );
}
