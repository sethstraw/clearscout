#!/usr/bin/env bash

set -e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
"$REPO_ROOT/scripts/tailorcv-local-app" stop
sleep 2
