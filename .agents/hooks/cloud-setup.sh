#!/usr/bin/env bash
# Usage: SessionStart hook. Provisions a Claude Code cloud VM with Python 3.12
# and Node 24, from uv.lock and web/package-lock.json; exits at once elsewhere.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
    exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"
# The cloud network policy refuses mise.run, so provisioning goes through the
# PyPI and npm registries only.
node_home="$HOME/.local/node24"
export PATH="$node_home/node_modules/.bin:$HOME/.local/bin:$PATH"

uv python install 3.12
if [ "$(node --version | cut -d. -f1)" != "v24" ]; then
    # The `node` wrapper package fails under npm's script policy; the platform
    # package ships bin/node with no install script.
    arch=$(uname -m | sed -e s/x86_64/x64/ -e s/aarch64/arm64/)
    npm install --prefix "$node_home" --no-audit --no-fund "node-linux-$arch@24"
fi
npm --prefix web ci --no-audit --no-fund
# The wheel force-includes web/dist, which a fresh clone lacks until the SPA builds.
npm --prefix web run build
uv sync --frozen
command -v just >/dev/null 2>&1 || uv tool install rust-just

# Later Bash calls in the session read PATH from this file.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "export PATH=\"$node_home/node_modules/.bin:$HOME/.local/bin:\$PATH\"" >>"$CLAUDE_ENV_FILE"
fi
