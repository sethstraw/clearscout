import { Link } from "react-router-dom";

/** The application path as links to the workspace pages that carry each step. */
export function SearchLoop() {
  return (
    <nav aria-label="Your application path" className="search-loop">
      <ol>
        <li>
          <strong>
            <Link to="/cv">My background</Link>
          </strong>
          <p>
            Import and review your evidence. A suggested role family is a search hypothesis, not a
            new career fact.
          </p>
        </li>
        <li>
          <strong>
            <Link to="/find?tab=boards">My search</Link>
          </strong>
          <p>
            <Link to="/settings?tab=search">Review search preferences</Link>, then scan the boards
            you already track. Search the web to find new jobs beyond those boards.
          </p>
        </li>
        <li>
          <strong>
            <Link to="/find?tab=review">My shortlist</Link>
          </strong>
          <p>
            Review promising discoveries, their evidence, and unanswered constraints before
            evaluating a target.
          </p>
        </li>
        <li>
          <strong>
            <Link to="/jobs">My application</Link>
          </strong>
          <p>
            Open an evaluated posting to select evidence, draft, review, and export an application.
          </p>
        </li>
      </ol>
    </nav>
  );
}
