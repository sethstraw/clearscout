set shell := ["bash", "-uc"]

default:
    @just --list

# Run one gate scope, `change` by default.
gate scope="change":
    scripts/gate.sh {{scope}}

# Regenerate license notices, the gazetteer, OpenAPI schema, TS client, and SPA build.
gen:
    scripts/generate.sh

# Python tests, parallel. The empty marker overrides the addopts filter, so this
# selects the recorded and transport tests the gate also runs.
test:
    uv run pytest -m ""

# One log directory per checkout, so a worktree and main never share a log.
pytest_log_dir := "/tmp/clearscout-pytest/" + file_name(justfile_directory())

# Focused tests into <pytest_log_dir>/<name>.log, ending with an exit=N line.
pytest-log name +files:
    mkdir -p {{pytest_log_dir}}
    uv run --no-sync pytest {{files}} --color=no -rfE -p no:cacheprovider > {{pytest_log_dir}}/{{name}}.log 2>&1; echo "exit=$?" >> {{pytest_log_dir}}/{{name}}.log
    @echo "log: {{pytest_log_dir}}/{{name}}.log $(tail -n 1 {{pytest_log_dir}}/{{name}}.log)"

# CI-strength property-test coverage (hypothesis: 100 examples, not 10).
test-ci:
    HYPOTHESIS_PROFILE=ci uv run pytest -m ""

# Ruff auto-fix and format for Python; Prettier for the repository.
fmt:
    uv run ruff check --fix .
    uv run ruff format .
    npm --prefix web run format 2>/dev/null || true

# SPA dev server.
spa:
    npm --prefix web run dev

# Backend API + worker (background). Stop with `just stop`.
start:
    uv run clearscout start

# Logs and the SPA pid sit beside the ones `clearscout start` writes, under the same resolved data root.
runtime_dir := "uv run --no-sync python -c 'from clearscout.bootstrap.local_runtime import RUNTIME_DIRECTORY; from clearscout.infrastructure.artifacts.runtime import RuntimeStorage; print(RuntimeStorage.resolve().data_root / RUNTIME_DIRECTORY)'"

# API, worker, and SPA in the background, each logging under the runtime directory.
dev-start:
    #!/usr/bin/env bash
    set -euo pipefail
    logs=$({{runtime_dir}})
    mkdir -p "$logs"
    uv run clearscout start
    if [ -f "$logs/spa.pid" ] && kill -0 "$(cat "$logs/spa.pid")" 2>/dev/null; then
        echo "SPA already running (pid $(cat "$logs/spa.pid"))."
    else
        if curl -sS --max-time 1 http://127.0.0.1:5173/ >/dev/null 2>&1; then
            echo "Port 5173 is already serving another SPA. Stop it before starting ClearScout." >&2
            exit 1
        fi
        # The SPA needs its own session to survive a non-interactive restart.
        spa_pid=$(uv run --no-sync python -c '
    import subprocess
    import sys
    from pathlib import Path

    web = Path("web").resolve()
    with (Path(sys.argv[1]) / "clearscout-spa.log").open("ab") as log:
        process = subprocess.Popen(
            [str(web / "node_modules/.bin/vite"), "--strictPort"],
            cwd=web,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(process.pid)
    ' "$logs")
        printf '%s\n' "$spa_pid" > "$logs/spa.pid"
        for attempt in {1..40}; do
            if ! kill -0 "$spa_pid" 2>/dev/null; then
                echo "SPA exited during startup. Logs: $logs/clearscout-spa.log" >&2
                exit 1
            fi
            if curl -fsS --max-time 1 http://127.0.0.1:5173/ >/dev/null 2>&1; then
                echo "SPA started (pid $spa_pid). Logs: $logs"
                exit 0
            fi
            sleep 0.25
        done
        echo "SPA did not become ready. Logs: $logs/clearscout-spa.log" >&2
        exit 1
    fi

# Stop the SPA, API, and worker that `just dev-start` launched.
dev-stop:
    #!/usr/bin/env bash
    set -uo pipefail
    logs=$({{runtime_dir}})
    if [ -f "$logs/spa.pid" ]; then
        kill "$(cat "$logs/spa.pid")" 2>/dev/null && echo "SPA stopped." || echo "SPA was not running."
        rm -f "$logs/spa.pid"
    fi
    uv run clearscout stop

dev-restart: dev-stop dev-start

# Follow the API, worker, and SPA logs.
dev-logs:
    #!/usr/bin/env bash
    logs=$({{runtime_dir}})
    tail -n 50 -F "$logs/clearscout-serve.log" "$logs/clearscout-worker.log" "$logs/clearscout-spa.log"

stop:
    uv run clearscout stop

status:
    uv run clearscout status

# API only, foreground (no worker).
serve:
    uv run clearscout serve

# Type checks (Python + SPA).
typecheck:
    uv run mypy
    npm --prefix web run typecheck

# One evaluation task against a canned provider (offline, free).
eval task="evals/inspect/tasks/match_scripted_task.py":
    scripts/eval.sh run {{task}}
