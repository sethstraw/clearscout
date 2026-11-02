import { Link } from "react-router-dom";

import { Page } from "../components/Page";

export function NotFoundPage() {
  return (
    <Page
      action={
        <Link className="button button--primary" to="/">
          Open Command Center
        </Link>
      }
      description="The local page you requested does not exist. No data was changed."
      eyebrow="404"
      title="Page not found"
    >
      <section className="panel not-found-panel">
        <h2>Try a known destination</h2>
        <p>Use the navigation, return to the Command Center, or open the API Playground.</p>
        <div className="button-row">
          <Link className="button button--quiet" to="/jobs">
            Jobs
          </Link>
          <Link className="button button--quiet" to="/api-playground">
            API Playground
          </Link>
        </div>
      </section>
    </Page>
  );
}
