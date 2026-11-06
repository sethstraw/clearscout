# Changelog

Changes a user would notice, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) form, versioned by [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- The critic runs at low effort on both providers: down from medium on OpenAI
  and from high on Anthropic.
- Requirement extraction asks OpenAI for low verbosity, and matching has a
  16000-token output ceiling instead of 8000.
- Matching no longer reads an asserted status line about another country or
  place as answering a requirement.
- Each claim the critic doubts quotes the evidence entry it compared against,
  and the report warns when that quote is not in your record.
- Drafting, cover-letter, and search-vocabulary schemas are constant, so the
  provider can cache them. The program refuses any cited identifier outside the
  set offered for that run.

## [0.1.0] - 2026-11-05

First public alpha, for technical early adopters. This is the first release;
the bullets below state the security properties 0.1.0 ships with. [README.md](README.md) and the
[user guide](docs/user-guide.md) describe what it does, and
[validation status](docs/validation.md) says how far each part has been tested.
Read that before relying on it: only the OpenAI path has run against a live
provider, board readers have uneven live coverage, and every drafted document
still needs your own line-by-line review. What the paid runs found is in
[evals/FINDINGS.md](evals/FINDINGS.md).

### Security

- The local server accepts only loopback Host authorities and exact
  same-origin browser writes, and keeps its content-security, no-store, frame,
  referrer, upload, and download protections.
- The browser never receives an API key. A key typed in Settings or stored with
  `clearscout auth set` is kept in the system keyring; the provider's
  environment variable or a one-run command-line option also works.
- Backups, the database, and artifacts are written owner-private.
- robots.txt decides every read except the first read of a posting URL you
  enter yourself, which still stops when robots.txt names ClearScout or its
  former name, TailorCV. Board
  and listing reads, refreshes, and every later read of a stored URL honor a
  robots disallow.

[Unreleased]: https://github.com/sethstraw/clearscout/compare/v0.1.0...main
[0.1.0]: https://github.com/sethstraw/clearscout/releases/tag/v0.1.0
