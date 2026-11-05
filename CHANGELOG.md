# Changelog

Changes a user would notice, in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) form, versioned by [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-05

First public alpha, for technical early adopters. There is no earlier release,
so this entry lists no changes. [README.md](README.md) and the
[user guide](docs/user-guide.md) describe what it does, and
[validation status](docs/validation.md) says how far each part has been tested.
Read that before relying on it: only the OpenAI path has run against a live
provider, board readers have uneven live coverage, and every drafted document
still needs your own line-by-line review. What the paid runs found is in
[evals/FINDINGS.md](evals/FINDINGS.md).

Entries start with the first change after this release.

### Security

- The local server accepts only loopback Host authorities and exact
  same-origin browser writes, and keeps its content-security, no-store, frame,
  referrer, upload, and download protections.
- The browser never receives an API key. A key is typed once and kept in the
  system keyring.
- Backups, the database, and artifacts are written owner-private.
- robots.txt decides every read except the first read of a posting URL you
  enter yourself, which still stops when robots.txt names ClearScout or its
  former name, TailorCV. Board
  and listing reads, refreshes, and every later read of a stored URL honor a
  robots disallow.

[Unreleased]: https://github.com/sethstraw/clearscout/compare/v0.1.0...main
[0.1.0]: https://github.com/sethstraw/clearscout/releases/tag/v0.1.0
