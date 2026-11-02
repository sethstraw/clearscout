#!/usr/bin/env bash
# Update generated artifacts.
# Usage: scripts/generate.sh

set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

uv run --locked python scripts/audit_licenses.py \
  --write-notices THIRD_PARTY_NOTICES.md \
  --write-license-texts THIRD_PARTY_LICENSES.txt
uv run --locked python scripts/build_gazetteer.py src/tailorcv/domain/gazetteer.json
uv run --locked python scripts/export_openapi.py web/openapi.json
npm --prefix web run api:generate
npm --prefix web run build
