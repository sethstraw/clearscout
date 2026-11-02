import { safeHttpUrl } from "../../api/safeUrl";
import { AggregatorCreditNote } from "../../components/AggregatorCredit";
import { constraintConfidenceRoute, type ConstraintConfidence } from "./constraintConfidence";

export type Discovery = {
  employer: string;
  location: string;
  note?: string;
  title: string;
  url: string;
  /** The address the row was discovered at, which names any aggregator owed credit. */
  creditUrl?: string;
  constraint_confidence?: ConstraintConfidence;
};

export function DiscoveryTable({ discoveries }: { discoveries: readonly Discovery[] }) {
  // A scan that reports no route for any row would otherwise show a column of
  // "Not reported", so the column appears only once something fills it.
  const anyConfidence = discoveries.some((item) =>
    Boolean(constraintConfidenceRoute(item.constraint_confidence)),
  );
  const anyNote = discoveries.some((item) => Boolean(item.note));
  return (
    <table aria-label="Discovered job postings" className="data-table">
      <thead>
        <tr>
          <th scope="col">Employer</th>
          <th scope="col">Role</th>
          <th scope="col">Location</th>
          {anyNote && <th scope="col">Publisher note</th>}
          {anyConfidence && <th scope="col">Local confidence route</th>}
        </tr>
      </thead>
      <tbody>
        {discoveries.map((item) => {
          const url = safeHttpUrl(item.url);
          const confidence = constraintConfidenceRoute(item.constraint_confidence);
          return (
            <tr key={item.url}>
              <td>{item.employer}</td>
              <td>
                {url ? (
                  <>
                    <a href={url} rel="noreferrer" target="_blank">
                      {item.title}
                    </a>
                    <AggregatorCreditNote url={item.creditUrl ?? url} />
                  </>
                ) : (
                  item.title
                )}
              </td>
              <td>{item.location}</td>
              {anyNote && <td>{item.note || "Not reported"}</td>}
              {anyConfidence && (
                <td>
                  {confidence ? (
                    <abbr title={confidence.definition}>{confidence.label}</abbr>
                  ) : (
                    "Not reported"
                  )}
                </td>
              )}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
