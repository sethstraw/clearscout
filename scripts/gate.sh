#!/usr/bin/env bash
# Verification gate.
# Usage: scripts/gate.sh [fast|change|related|py|web|generated|security|package|workflows|push|all]
#
# A check runs at the frequency its answer can change. `change` is the default
# and covers everything a code edit can break. `related` narrows the test suites
# to files the branch touched or that import them, for slow machines; it does
# not replace `change` before a merge. `push` covers what an edit rarely
# touches and the world changes underneath: the distribution, published
# advisories, and the workflows. `all` is both.
#
# GATE_FIX=1 runs ruff, prettier, eslint, knip, and zizmor in auto-fix mode
# before the check step. Off by default.

set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

SCOPE="${1:-change}"
GATE_FIX="${GATE_FIX:-0}"
FAILED=()
TMPDIRS=()
PYTHON_BIN="${PYTHON:-}"

cleanup() {
  if ((${#TMPDIRS[@]})); then
    for dir in "${TMPDIRS[@]}"; do
      rm -rf "$dir"
    done
  fi
}
trap cleanup EXIT

step() {
  local label="$1"
  shift
  printf '\n== %s\n' "$label"
  if "$@"; then
    return 0
  fi
  printf '\n!! %s failed\n' "$label"
  FAILED+=("$label")
  return 0
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    return 1
  fi
}

check_prerequisites() {
  local missing=0
  for command in bash diff git mktemp npm uv uvx; do
    require_command "$command" || missing=1
  done
  if [[ -n "$PYTHON_BIN" ]]; then
    require_command "$PYTHON_BIN" || missing=1
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    printf 'Missing required command: python or python3\n' >&2
    missing=1
  fi
  if [[ ! -f web/package-lock.json ]]; then
    printf 'Missing web/package-lock.json; run npm ci --prefix web after restoring it.\n' >&2
    missing=1
  fi
  if [[ ! -f uv.lock ]]; then
    printf 'Missing uv.lock; restore it before running the gate.\n' >&2
    missing=1
  fi
  if ((missing)); then
    exit 69
  fi
}

tmpdir() {
  local dir
  dir="$(mktemp -d)"
  TMPDIRS+=("$dir")
  printf '%s\n' "$dir"
}

uv_locked() {
  uv run --locked "$@"
}

check_license_inventory() {
  local dir="$1"
  uv_locked python scripts/audit_licenses.py \
    --write-notices "$dir/THIRD_PARTY_NOTICES.md" \
    --write-license-texts "$dir/THIRD_PARTY_LICENSES.txt" &&
    diff -u THIRD_PARTY_NOTICES.md "$dir/THIRD_PARTY_NOTICES.md" &&
    diff -u THIRD_PARTY_LICENSES.txt "$dir/THIRD_PARTY_LICENSES.txt"
}

check_openapi() {
  local dir="$1"
  uv_locked python scripts/export_openapi.py "$dir/openapi.json" &&
    diff -u web/openapi.json "$dir/openapi.json"
}

check_gazetteer() {
  local dir="$1"
  uv_locked python scripts/build_gazetteer.py "$dir/gazetteer.json" &&
    diff -u src/clearscout/domain/gazetteer.json "$dir/gazetteer.json"
}

check_typescript_contract() {
  local dir="$1"
  npm --prefix web exec openapi-typescript -- "$dir/openapi.json" -o "$dir/schema.d.ts" &&
    diff -u web/src/api/schema.d.ts "$dir/schema.d.ts"
}

check_distribution() {
  uv build &&
    "$PYTHON_BIN" scripts/verify_distribution.py dist/*.whl dist/*.tar.gz &&
    uvx --from check-wheel-contents==0.6.3 check-wheel-contents dist/*.whl
}

check_installed_wheel() {
  local dir="$1"
  local root="$PWD"
  uv venv --quiet "$dir/venv" &&
    uv pip install --quiet --python "$dir/venv/bin/python" dist/*.whl &&
    "$dir/venv/bin/clearscout" --version &&
    "$dir/venv/bin/clearscout" --help >/dev/null &&
    mkdir "$dir/smoke" &&
    (
      cd "$dir/smoke" &&
        "$dir/venv/bin/clearscout" template CV_Master.md &&
        test -s CV_Master.md &&
        "$dir/venv/bin/python" -I "$root/scripts/smoke_installed_frontend.py"
    )
}

run_fast() {
  if [[ "$GATE_FIX" == "1" ]]; then
    uv_locked ruff format src tests scripts evals >/dev/null || true
    uv_locked ruff check --fix src tests scripts evals >/dev/null || true
  fi
  step "ruff format" uv_locked ruff format --check src tests scripts evals
  step "ruff check" uv_locked ruff check src tests scripts evals
  step "mypy" uv_locked mypy
  step "import contracts" uv_locked lint-imports
  # No snapshot is consulted, so the ceiling is the whole rule and the repository
  # carries no per-function watermark to drift.
  step "cognitive complexity" uv_locked complexipy src \
    --max-complexity-allowed 20 --snapshot-ignore --failed --color no
}

run_python() {
  run_fast
  step "deptry" uv_locked deptry src
  # The empty marker expression overrides the addopts filter, so the recorded
  # and transport tests are selected here.
  step "pytest" env HYPOTHESIS_PROFILE=ci \
    uv run --locked pytest -m "" --cov=clearscout --cov-report=term-missing
  step "evals" uv_locked python -m evals
}

run_web() {
  if [[ "$GATE_FIX" == "1" ]]; then
    npm --prefix web run format >/dev/null || true
    npm --prefix web run lint -- --fix >/dev/null || true
    npm --prefix web exec knip -- --fix >/dev/null || true
  fi
  step "prettier" npm --prefix web run format:check
  step "eslint" npm --prefix web run lint
  step "knip" npm --prefix web run knip
  step "vitest" npm --prefix web test
  step "tsc" npm --prefix web run typecheck
  step "build the spa" npm --prefix web run build
}

run_generated() {
  local dir
  dir="$(tmpdir)"
  step "license inventory drift" check_license_inventory "$dir"
  step "openapi drift" check_openapi "$dir"
  step "gazetteer index drift" check_gazetteer "$dir"
  step "typescript contract drift" check_typescript_contract "$dir"
  step "public data audit" uv_locked python scripts/audit_public_data.py
  step "markdown links" uv_locked python scripts/check_markdown_links.py
  step "comment policy" uv_locked python scripts/check_comment_policy.py
}

# Committed and uncommitted paths that differ from where the branch left main.
changed_paths() {
  local base
  base="$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main)"
  {
    git diff --name-only "$base"
    git ls-files --others --exclude-standard
  } | sort -u
}

# Test files naming the module directly, or importing it from its parent package.
importers_of() {
  local module="$1"
  local parent="${module%.*}" name="${module##*.}" file
  grep -rlE --include='*.py' "${module//./\\.}([^[:alnum:]_]|$)" tests
  grep -rlE --include='*.py' "from ${parent//./\\.} import" tests |
    while IFS= read -r file; do
      grep -qw -- "$name" "$file" && printf '%s\n' "$file"
    done
}

# A helper or conftest selects its whole directory, since its users are implicit.
as_test_target() {
  case "$(basename "$1")" in
    test_*.py) printf '%s\n' "$1" ;;
    *) dirname "$1" ;;
  esac
}

related_python_tests() {
  local path module hit
  changed_paths | while IFS= read -r path; do
    case "$path" in
      tests/*.py)
        [[ -e "$path" ]] && as_test_target "$path"
        ;;
      src/clearscout/*.py)
        module="${path#src/}"
        module="${module%.py}"
        module="${module%/__init__}"
        importers_of "${module//\//.}" | while IFS= read -r hit; do
          as_test_target "$hit"
        done
        ;;
    esac
  done | sort -u
}

run_related_python() {
  local tests=() target
  while IFS= read -r target; do
    tests+=("$target")
  done < <(related_python_tests)
  if ((${#tests[@]} == 0)); then
    printf '\n== related pytest: no test names a changed module\n'
    return 0
  fi
  step "related pytest" env HYPOTHESIS_PROFILE=ci uv run --locked pytest -m "" "${tests[@]}"
}

# npm exec keeps the caller's directory, so vitest needs web/ as its root.
related_vitest() {
  npm --prefix web exec -- vitest related --root web --run "$@"
}

run_related_web() {
  local sources=() path
  while IFS= read -r path; do
    [[ -e "$path" ]] && sources+=("$PWD/$path")
  done < <(changed_paths | grep '^web/src/')
  if ((${#sources[@]} == 0)); then
    return 0
  fi
  step "prettier" npm --prefix web run format:check
  step "eslint" npm --prefix web run lint
  step "related vitest" related_vitest "${sources[@]}"
  step "tsc" npm --prefix web run typecheck
}

run_security() {
  step "python advisory audit" uv_locked pip-audit --local --skip-editable --progress-spinner=off
  step "platform advisory audit" uv_locked pip-audit \
    --requirement scripts/platform-conditional-requirements.txt \
    --disable-pip \
    --progress-spinner=off
  step "web dependency audit" uv_locked python scripts/audit_npm_security.py
  step "license inventory" check_license_inventory "$(tmpdir)"
}

run_package() {
  step "build and inspect distributions" check_distribution
  step "installed first-use smoke test" check_installed_wheel "$(tmpdir)"
}

run_workflows() {
  if [[ "$GATE_FIX" == "1" ]]; then
    uvx zizmor@1.30.1 --persona=regular --fix=safe .github/workflows >/dev/null 2>&1 || true
  fi
  step "workflow lint" uvx zizmor@1.30.1 --persona=regular .github/workflows
}

check_eval_harness_imports() {
  uv_locked python -c "import evals.inspect.runner, evals.inspect.tasks.match_task, evals.inspect.tasks.match_scripted_task, evals.inspect.tasks.match_scripted_multi_task, evals.inspect.tasks.match_live_task, evals.inspect.build_calibration, evals.inspect.cost_estimate, evals.inspect.diff"
}

run_evals_imports() {
  step "eval harness imports" check_eval_harness_imports
}

check_prerequisites

case "$SCOPE" in
  fast)
    run_fast
    ;;
  py)
    run_python
    ;;
  web)
    run_web
    ;;
  generated)
    run_generated
    ;;
  security)
    run_security
    ;;
  package)
    run_web
    run_package
    ;;
  workflows)
    run_workflows
    ;;
  change)
    run_python
    run_web
    run_generated
    ;;
  related)
    run_fast
    run_related_python
    run_related_web
    run_generated
    ;;
  push)
    run_security
    run_package
    run_workflows
    run_evals_imports
    ;;
  all)
    run_python
    run_web
    run_generated
    run_security
    run_package
    run_workflows
    run_evals_imports
    ;;
  *)
    printf 'Unknown scope %s. Use fast, change, related, py, web, generated, security, package, workflows, push, or all.\n' "$SCOPE" >&2
    exit 64
    ;;
esac

if ((${#FAILED[@]})); then
  printf '\n== gate failed: %s\n' "${FAILED[*]}"
  exit 1
fi

printf '\n== gate passed\n'
