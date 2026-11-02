/** The order the Find page expects: scan free first, then pay to find or widen. */
export function FindSequence() {
  return (
    <section aria-label="Ways to find jobs" className="search-loop search-loop--choices">
      <h2>Ways to find jobs</h2>
      <ul>
        <li>
          <strong>Scan for openings</strong>
          <p>Scans the boards you already track and stages current openings for review.</p>
          <span className="badge">Free</span>
        </li>
        <li>
          <strong>Search the web for new jobs</strong>
          <p>
            Finds new postings and boards beyond your saved boards. You approve an estimate before
            it runs.
          </p>
          <span className="badge badge--paid">
            Paid; per-search content charges are provider-set
          </span>
        </li>
        <li>
          <strong>Propose new search terms</strong>
          <p>
            One CV-grounded call that finds no jobs; it widens what later scans and web searches
            look for.
          </p>
          <span className="badge badge--paid">Paid; optional widener</span>
        </li>
      </ul>
    </section>
  );
}
