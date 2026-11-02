import { Link } from "react-router-dom";

import { useMasterCvVersions, useSearchParameters, useSources } from "../../api/hooks";
import { Icons } from "../../components/Icons";

/** The four steps before the first results, until all four are done. */
export function FirstRunChecklist({ workspaceId }: { workspaceId: string }) {
  const versions = useMasterCvVersions(workspaceId);
  const parameters = useSearchParameters(workspaceId);
  const sources = useSources(workspaceId);
  if (!versions.isSuccess || !parameters.isSuccess || !sources.isSuccess) return null;
  const boards = sources.data.items;
  const steps = [
    { label: "Add your CV", to: "/cv", done: versions.data.length > 0 },
    {
      label: "Add target titles",
      to: "/settings",
      done: (parameters.data.parameters.seniority?.target_titles?.length ?? 0) > 0,
    },
    {
      label: "Add a job board",
      to: "/find?tab=boards",
      done: boards.some((board) => board.enabled),
    },
    {
      label: "Run your first scan",
      to: "/find?tab=boards",
      done: boards.some((board) => board.scan_count > 0),
    },
  ];
  const remaining = steps.filter((step) => !step.done).length;
  if (remaining === 0) return null;
  return (
    <section aria-labelledby="first-run-heading" className="panel first-run">
      <header>
        <div>
          <h2 id="first-run-heading">Get started</h2>
          <p>
            {remaining} of {steps.length} steps left. A Markdown CV is free. Other formats use your
            provider.
          </p>
        </div>
      </header>
      <ol className="first-run__steps">
        {steps.map((step) => (
          <li className={step.done ? "first-run__step done" : "first-run__step"} key={step.label}>
            {step.done ? (
              <Icons.check aria-hidden="true" height={16} width={16} />
            ) : (
              <Icons.circle aria-hidden="true" height={16} width={16} />
            )}
            {step.done ? (
              <span>
                {step.label} <span className="visually-hidden">(done)</span>
              </span>
            ) : (
              <Link to={step.to}>{step.label}</Link>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
