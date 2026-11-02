import { Link } from "react-router-dom";

import { Page } from "../components/Page";

export function NotFoundPage() {
  return (
    <Page
      action={
        <Link className="button button--primary" to="/">
          Open Home
        </Link>
      }
      description="This page does not exist. No data was changed."
      eyebrow="404"
      title="Page not found"
    >
      <section className="panel not-found-panel">
        <h2>Try a known page</h2>
        <p>Use the navigation, or open one of these.</p>
        <div className="button-row">
          <Link className="button button--quiet" to="/jobs">
            Jobs
          </Link>
          <Link className="button button--quiet" to="/settings?tab=advanced&section=diagnostics">
            Diagnostics
          </Link>
        </div>
      </section>
    </Page>
  );
}
