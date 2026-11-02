import { Fragment } from "react";

import { EMPLOYER_NOT_STATED, postingHost } from "./fallbacks";

/** A host that line-breaks only after its dots, so a narrow column never splits a label. */
export function HostName({ host }: { host: string }) {
  const labels = host.split(".");
  return (
    <>
      {labels.map((label, index) => (
        <Fragment key={index}>
          {label}
          {index < labels.length - 1 ? (
            <>
              .<wbr />
            </>
          ) : null}
        </Fragment>
      ))}
    </>
  );
}

/** A stored employer, or a quiet not-stated placeholder with the listing host as its source. */
export function EmployerName({
  employer,
  url,
}: {
  employer: string | null | undefined;
  url: string | null;
}) {
  if (employer) return <>{employer}</>;
  const host = postingHost(url);
  return (
    <>
      <p className="field-note employer-not-stated">{EMPLOYER_NOT_STATED}</p>
      {host ? (
        <p className="field-note employer-not-stated">
          Listed on <HostName host={host} />
        </p>
      ) : null}
    </>
  );
}
