# Version-bound dependency reviews

This is a record of conservative engineering review, not legal advice.
`scripts/audit_licenses.py` binds four reviews to an exact version in
`REVIEWED_EXCEPTIONS`. certifi, pypdfium2, tld, and tqdm declare expressions the
distributed policy refuses, so the gate fails when any of them changes version.

Build, lint, type and test tooling is held to a reduced policy that permits
copyleft, because no wheel, sdist or SPA bundle contains it. A development
package still fails when its metadata declares no usable grant. That tier covers
`pathspec` (MPL-2.0, reached through mypy), `axe-core` (MPL-2.0, reached through
eslint-plugin-jsx-a11y), `caniuse-lite` (CC-BY-4.0, browserslist data), and the
`lightningcss` platform family (MPL-2.0, Vite's CSS transform), so none of them
needs an entry here. The reduced policy is not an
absence of policy: a development package still fails on AGPL, SSPL, or metadata
declaring no usable grant.

Python notices are the union of the installed tree and exact lockfile packages
selected only on Linux or Windows. The latter are normalized through
`scripts/platform-conditional-requirements.txt`, so macOS generation and Linux
CI produce the same complete inventory. Which of them are distributed comes from
`uv export --no-dev --frozen`, so the tier is derived from the lockfile rather
than from a list kept by hand.

| Dependency | Version    | Use                                        | Review decision                                                                                                                                                              |
| ---------- | ---------- | ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| certifi    | 2026.10.23 | Python runtime CA bundle                   | MPL-2.0 files are consumed unmodified; retain upstream license and notices.                                                                                                  |
| pypdfium2  | 5.14.0     | Python runtime PDF ingestion               | Metadata names BSD-3-Clause, Apache-2.0, and bundled dependency licenses; upstream distribution supplies the applicable notices. Preserve them when redistributing binaries. |
| tld        | 0.13.2     | Transitive URL/date extraction dependency  | The expression offers MPL-1.1 as an alternative to GPL/LGPL; this project selects the MPL alternative and does not modify package files.                                     |
| tqdm       | 4.70.0     | Transitive Python runtime progress utility | MPL-2.0 and MIT metadata reviewed; consumed unmodified with upstream notices retained.                                                                                       |

## Bundled packages with no notice file

Some bundled web packages ship no LICENSE, COPYING or NOTICE file at all, and
state their grant only in the package manifest. `scripts/audit_licenses.py`
records each one in `THIRD_PARTY_LICENSES.txt` as "no notice file shipped",
naming the declared expression, rather than supplying a license text on the
publisher's behalf. The package still has to pass the permissive allowlist, and
the record follows the installed version with no version binding. These are the
packages in that state:

| Dependency                     | Use                                 | Declared    | Note                                                                                              |
| ------------------------------ | ----------------------------------- | ----------- | ------------------------------------------------------------------------------------------------- |
| @uiw/react-md-editor           | Bundled Markdown editor             | MIT         | Links the same upstream repository as the renderer below.                                         |
| @uiw/react-markdown-preview    | Bundled Markdown renderer           | MIT         | Links the same upstream repository as the editor above.                                           |
| boolbase                       | Bundled transitive selector utility | ISC         | No README license section either. Two lines of code, reached through the editor's HTML sanitizer. |
| remark-github-blockquote-alert | Bundled transitive Markdown plugin  | MIT         | Reached through @uiw/react-markdown-preview.                                                      |
| victory-vendor                 | Bundled chart runtime               | MIT AND ISC | The separately inventoried D3 and InternMap packages supply the ISC and BSD texts.                |

The generated [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) names the
installed version of each.

## Peer-range exception

`eslint-plugin-jsx-a11y` 6.10.2 declares a peer range ending at eslint 9, and
this repository is on eslint 10. `web/package.json` carries an npm `overrides`
entry for that package alone, which makes it follow the root eslint version. The compatibility is asserted here
rather than upstream, so it was checked rather than assumed: linting a component
with an alt-less image and a click handler on a plain div reports
`jsx-a11y/alt-text`, `click-events-have-key-events` and
`no-static-element-interactions`, so the rules do run. Remove the override when
upstream widens the range, and re-check enforcement the same way if either
version moves.

A second override lifts every other `minimatch` consumer to 11.0.3 for
GHSA-g3hv-75wg-m249. jsx-a11y 6.10.2 calls minimatch 3's default export and
breaks on 11, so the same nested entry keeps that package alone on 3.1.5. The
whole-tree audit reports that copy as a moderate development advisory, which
the audit permits.

## Security audit

`scripts/audit_npm_security.py` fails on any production npm advisory and on
any high or critical advisory in a development dependency. No advisory is
excepted.

## Generated and original assets

`web/openapi.json` is generated from ClearScout's own FastAPI schema.
`web/src/api/schema.d.ts` is generated by openapi-typescript 7.13.0 (MIT).
The ClearScout mark, layout, CSS, copy, and inline SVG icons were created for this
project and remain under ClearScout's Apache-2.0 license. No external fonts or images are
redistributed.

`src/clearscout/domain/gazetteer.json` is generated by `scripts/build_gazetteer.py`
from GeoNames data (https://www.geonames.org/), as packaged by the dev-only
geonamescache 3.0.2 (MIT). The GeoNames data is licensed under Creative Commons
Attribution 4.0 International (https://creativecommons.org/licenses/by/4.0/);
the source is the cities15000 extract at
https://download.geonames.org/export/dump/. ClearScout modified it: it keeps only
that extract (population of fifteen thousand or more) and primary names, folds
them to ASCII, removes names of three letters or fewer, and trims the fields to
country, US or Canadian subdivision, and population. The data is provided as is,
without warranty of any kind. The same attribution is the file's top-level
`attribution` field, which is the copy that ships in the wheel.
