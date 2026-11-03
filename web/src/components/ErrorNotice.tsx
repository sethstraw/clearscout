import { Link } from "react-router-dom";

import { errorNotice } from "./freeMode";

/** Inline text for a failed request, with the route that resolves it when there is one. */
export function ErrorNotice({ error }: { error: Error }) {
  const { message, action } = errorNotice(error);
  return (
    <>
      {message}
      {action && (
        <>
          {" "}
          <Link to={action.to}>{action.label}</Link>
        </>
      )}
    </>
  );
}
