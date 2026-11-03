#!/usr/bin/env bash

set -e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec "$REPO_ROOT/scripts/clearscout-local-app" start
