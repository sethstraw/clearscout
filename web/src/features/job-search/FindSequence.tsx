/** The order the Find page expects: scan saved boards first, then search wider. */
export function FindSequence() {
  return (
    <section aria-label="Ways to find jobs" className="search-loop search-loop--choices">
      <h2>Ways to find jobs</h2>
      <ul>
        <li>
          <strong>Scan for openings</strong>
          <p>Scans the boards you already track and stages current openings for review.</p>
        </li>
        <li>
          <strong>Search the web for new jobs</strong>
          <p>
            Finds new postings and boards beyond your saved boards. You approve an estimate before
            it runs.
          </p>
        </li>
        <li>
          <strong>Propose new search terms</strong>
          <p>
            One CV-grounded call that finds no jobs; it widens what later scans and web searches
            look for.
          </p>
        </li>
      </ul>
    </section>
  );
}
