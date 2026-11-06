#!/usr/bin/env bash
# Prompt-quality evaluation entry point.
# Usage: scripts/eval.sh <subcommand> [args...]
#
# Subcommands:
#   build-calibration --data-root <path> --output <private-path>
#     Emit rulings paired with the current classification. These are
#     calibration data, not gold labels.
#
#   run <task_file>
#     Run 'python -m evals.inspect.runner <task_file> --log-dir reports/eval'.
#     The match-scripted task (the just eval default) uses a canned provider;
#     the match-replay task uses the replay solver. Neither tests quality.
#
#   diff <baseline_log> <candidate_log>
#     Print passes out of trials per case for each log (or arm), with tokens and
#     dollars per log.
#
#   --rescore <task_file> <log.jsonl> [--output <path>] [--allow-dataset-change]
#     Re-apply the task's current scorer to a finished log's stored solver
#     outputs and write a new log (default <log>.rescored.jsonl). Calls no
#     provider and spends nothing. A dataset whose bytes differ from the run's
#     is refused unless --allow-dataset-change is passed.
#
#   --live <task_file> --data-root <path> --limit N --split <split>
#     --provider <provider> --i-approve $X.XX --output-dir <private-path> [--trials N]
#     [--model <id>] [--effort <level>] [--ids <id,id,...>] [--no-smoke]
#     [--prompt-arm <name>=<private-dir> ...] [--cv-version <id>]
#     [--selection-manifest <private-path> --selection-manifest-sha256 <sha256>
#     --selection-manifest-bytes-sha256 <sha256>] [--dataset <private-path>]
#     --dataset runs the task on that JSONL instead of its tracked fixture, and the
#     run records that file's sha256.
#     A run of more than one call first smoke-tests the first sample once through
#     the same solver and scorer, prints 'smoke: ok' with the projected cost or
#     'smoke: failed <reason>', and stops on a failure; the passing call is reused
#     as that sample's first trial. --no-smoke skips it; batching solvers skip it.
#     --model sends that model on the task's own stage instead of the shipped
#     one, and the estimate reads only that model's history.
#     --effort does the same for the stage's effort level, which the model must
#     accept; the estimate reads only history at that effort.
#     --trials repeats each selected sample N times against one approval, which
#     is how a non-deterministic stage is measured without re-approving a run.
#     --ids names the samples to run instead of taking the first --limit of the
#     split, for an approval that names particular cases. --limit stays required
#     and states the approved count, which the solver refuses to disagree with.
#     Run one live evaluation. The stage is read from the task file's own STAGE
#     declaration, so the estimate and the run cannot name different stages.
#     Estimates the run's cost from the workspace's own model_calls history (last
#     20 calls for that stage and model) and requires explicit --i-approve consent;
#     monetary estimates never stop the run. --live is the only mode that tests
#     prompt-quality regressions and the only one that spends. The match stage
#     scores a selection manifest, so it also requires the
#     selection-manifest triple; every other stage refuses one.
#     --prompt-arm declares one prompt version as a directory holding one
#     <prompt>.md per prompt the stage uses. Repeat it to interleave arms in one
#     run: each sample then runs --trials times per arm, every call is checked
#     against the bytes its arm declared, and every row names its arm. Only the
#     relevance_review and jobspec stages take prompt arms.
#     --cv-version reviews against that master-CV version from the --data-root
#     workspace instead of the synthetic CV, and the run metadata records it. The
#     task's dataset must then sit outside the repository, and every row's cv_hash
#     must equal the content hash the version parses to. relevance_review only.
#
#   --live <task_file> --data-root <path> --provider <provider> --render-only <private-dir>
#     [--model <id>] [--effort <level>] [--cv-version <id>]
#     [--limit N | --ids <id,id,...>] [--split <split>]
#     [--prompt-arm <name>=<private-dir> ...]
#     Write, for every batch the first trial sends under every arm, the
#     instructions, evidence, and task text a live relevance_review run would
#     send, with their digests, member sample ids, posting span ids, CV entry ids,
#     and response-schema digest. Calls no provider, needs no credential or
#     approval, and writes no cost record.

set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

LOG_DIR="reports/eval"

usage() {
  cat >&2 <<'EOF'
Usage: scripts/eval.sh <subcommand> [args...]

Subcommands:
  build-calibration --data-root <path> --output <private-path>
  run <task_file>
  diff <baseline_log> <candidate_log>
  --rescore <task_file> <log.jsonl> [--output <path>] [--allow-dataset-change]
  --live <task_file> --data-root <path> --limit N --split <split> --provider <provider> \
    --i-approve $X.XX --output-dir <private-path> [--trials N] [--model <id>] \
    [--effort <level>] [--ids <id,id,...>] [--prompt-arm <name>=<private-dir> ...] \
    [--cv-version <id>] [--selection-manifest <private-path> \
    --selection-manifest-sha256 <sha256> --selection-manifest-bytes-sha256 <sha256>] \
    [--dataset <private-path>] [--no-smoke]
  --live <task_file> --data-root <path> --provider <provider> --render-only <private-dir> \
    [--model <id>] [--effort <level>] [--cv-version <id>] \
    [--limit N | --ids <id,id,...>] [--split <split>] [--prompt-arm <name>=<private-dir> ...]

The selection-manifest triple is required for the match stage and refused for
every other stage. Prompt arms are taken by the relevance_review and jobspec
stages; --cv-version and --render-only only by relevance_review.
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 64
fi

SUBCOMMAND="$1"
shift

validate_prompt_arm() {
  local entry="$1" name="${1%%=*}" directory="${1#*=}"
  if [[ "$entry" != *=* || -z "$name" || -z "$directory" ]]; then
    printf 'Refused: --prompt-arm must be name=directory: %s\n' "$entry" >&2
    exit 64
  fi
  if [[ ! "$name" =~ ^[a-z][a-z0-9-]*$ ]]; then
    printf 'Refused: --prompt-arm name must be lowercase letters, digits, or dashes: %s\n' \
      "$name" >&2
    exit 64
  fi
  # The arms reach the run as one comma-separated variable.
  if [[ "$entry" == *,* ]]; then
    printf 'Refused: --prompt-arm cannot contain a comma: %s\n' "$entry" >&2
    exit 64
  fi
  if [[ ! -d "$directory" ]]; then
    printf 'Refused: --prompt-arm directory does not exist: %s\n' "$directory" >&2
    exit 64
  fi
}

live_run() {
  local task_file="" data_root="" limit="" split="" provider="" approve=""
  local output_dir="" stage="" trials="" ids="" model="" effort=""
  local selection_manifest="" selection_manifest_sha256=""
  local selection_manifest_bytes_sha256=""
  local prompt_arms="" prompt_arm_names="" arms=0 cv_version="" render_dir="" dataset=""
  local smoke=1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --data-root|--limit|--ids|--split|--provider|--i-approve|\
      --output-dir|--trials|--model|--effort|--prompt-arm|--selection-manifest|\
      --selection-manifest-sha256|--selection-manifest-bytes-sha256|--cv-version|\
      --render-only|--dataset)
        if [[ $# -lt 2 || "$2" == --* ]]; then
          printf 'Missing value for --live option: %s\n' "$1" >&2
          exit 64
        fi
        ;;
    esac
    case "$1" in
      --data-root) data_root="$2"; shift 2 ;;
      --limit) limit="$2"; shift 2 ;;
      --ids) ids="$2"; shift 2 ;;
      --split) split="$2"; shift 2 ;;
      --provider) provider="$2"; shift 2 ;;
      --i-approve) approve="$2"; shift 2 ;;
      --output-dir) output_dir="$2"; shift 2 ;;
      --trials) trials="$2"; shift 2 ;;
      --model) model="$2"; shift 2 ;;
      --effort) effort="$2"; shift 2 ;;
      --prompt-arm)
        validate_prompt_arm "$2"
        local arm_name="${2%%=*}"
        case ",$prompt_arm_names," in
          *",$arm_name,"*)
            printf 'Refused: duplicate --prompt-arm name: %s\n' "$arm_name" >&2
            exit 64
            ;;
        esac
        prompt_arm_names="${prompt_arm_names:+$prompt_arm_names,}$arm_name"
        prompt_arms="${prompt_arms:+$prompt_arms,}$2"
        arms=$((arms + 1))
        shift 2
        ;;
      --selection-manifest) selection_manifest="$2"; shift 2 ;;
      --selection-manifest-sha256) selection_manifest_sha256="$2"; shift 2 ;;
      --selection-manifest-bytes-sha256) selection_manifest_bytes_sha256="$2"; shift 2 ;;
      --cv-version) cv_version="$2"; shift 2 ;;
      --render-only) render_dir="$2"; shift 2 ;;
      --dataset) dataset="$2"; shift 2 ;;
      --no-smoke) smoke=0; shift ;;
      --*)
        printf 'Unknown --live option: %s\n' "$1" >&2
        exit 64
        ;;
      *)
        if [[ -z "$task_file" ]]; then task_file="$1"; shift; else
          printf 'Unexpected --live argument: %s\n' "$1" >&2
          exit 64
        fi
        ;;
    esac
  done
  if [[ -n "$render_dir" ]]; then
    if [[ -n "$approve$output_dir$trials" ]]; then
      printf 'Refused: --render-only renders one trial and spends nothing; it takes no --i-approve, --output-dir, or --trials.\n' >&2
      exit 64
    fi
    if [[ -n "$dataset" ]]; then
      printf 'Refused: --render-only renders the task fixture and does not take --dataset.\n' >&2
      exit 64
    fi
    if [[ -z "$task_file" || -z "$data_root" || -z "$provider" ]]; then
      usage
      exit 64
    fi
  elif [[ -z "$task_file" || -z "$data_root" || -z "$limit" || -z "$split" || \
        -z "$provider" || -z "$approve" || -z "$output_dir" ]]; then
    usage
    exit 64
  fi
  if [[ ! -f "$task_file" ]]; then
    printf 'Refused: no task file at %s\n' "$task_file" >&2
    exit 66
  fi
  # The task file owns its stage, so the estimate and the run cannot disagree.
  stage="$(sed -n 's/^STAGE = "\([a-z_]*\)"$/\1/p' "$task_file" | head -1)"
  case "$stage" in
    match|relevance_review|jobspec|draft|cover|critic|posting_recovery|guided_campaign|search_vocabulary) ;;
    "")
      printf 'Refused: %s declares no STAGE.\n' "$task_file" >&2
      exit 64
      ;;
    *)
      printf 'Refused: %s declares an unknown STAGE: %s\n' "$task_file" "$stage" >&2
      exit 64
      ;;
  esac
  # SelectionManifestBody pins the ordered_cases count and carries the match
  # extraction artifacts, so no other family can present a valid manifest.
  if [[ "$stage" == match ]]; then
    if [[ -z "$selection_manifest" || -z "$selection_manifest_sha256" || \
          -z "$selection_manifest_bytes_sha256" ]]; then
      usage
      exit 64
    fi
    if [[ ! "$selection_manifest_sha256" =~ ^[0-9a-f]{64}$ ]]; then
      printf 'Refused: --selection-manifest-sha256 must be lowercase SHA-256.\n' >&2
      exit 64
    fi
    if [[ ! "$selection_manifest_bytes_sha256" =~ ^[0-9a-f]{64}$ ]]; then
      printf 'Refused: --selection-manifest-bytes-sha256 must be lowercase SHA-256.\n' >&2
      exit 64
    fi
  elif [[ -n "$selection_manifest$selection_manifest_sha256$selection_manifest_bytes_sha256" ]]; then
    printf 'Refused: only the match stage takes a selection manifest.\n' >&2
    exit 64
  fi
  # Only these solvers read the arm declaration, so another stage would label
  # rows with arms every call shared.
  if [[ -n "$prompt_arms" && "$stage" != relevance_review && "$stage" != jobspec ]]; then
    printf 'Refused: only the relevance_review and jobspec stages take prompt arms.\n' >&2
    exit 64
  fi
  if [[ -n "$cv_version$render_dir" && "$stage" != relevance_review ]]; then
    printf 'Refused: only the relevance_review stage takes --cv-version or --render-only.\n' >&2
    exit 64
  fi
  if [[ -n "$cv_version" && ! "$cv_version" =~ ^[A-Za-z0-9._-]+$ ]]; then
    printf 'Refused: --cv-version must be one master-CV version id.\n' >&2
    exit 64
  fi
  local database_url="sqlite+pysqlite:///${data_root%/}/clearscout.sqlite3"
  if [[ -n "$render_dir" ]]; then
    # The render selects rows as the paid run would, so it proves that run's batches.
    local -a render_selection=()
    if [[ -n "$ids" ]]; then
      local -a render_ids
      IFS=',' read -ra render_ids <<< "$ids"
      render_selection+=(--ids "${render_ids[@]}")
    fi
    if [[ -n "$limit" ]]; then render_selection+=(--limit "$limit"); fi
    if [[ -n "$split" ]]; then render_selection+=(--split "$split"); fi
    CLEARSCOUT_EVAL_LIVE_MODEL="$model" \
      CLEARSCOUT_EVAL_LIVE_EFFORT="$effort" \
      CLEARSCOUT_EVAL_PROMPT_ARMS="$prompt_arms" \
      CLEARSCOUT_EVAL_CV_VERSION="$cv_version" \
      CLEARSCOUT_EVAL_DATABASE_URL="$database_url" \
      uv run python -m evals.inspect.solvers.relevance_live "$task_file" \
        --render-only "$render_dir" --provider "$provider" \
        ${render_selection[@]+"${render_selection[@]}"}
    return
  fi
  if [[ "$arms" -eq 0 ]]; then
    arms=1
  fi
  if [[ ! "$limit" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Refused: --limit must be a positive integer.\n' >&2
    exit 64
  fi
  if [[ -n "$ids" && ! "$ids" =~ ^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$ ]]; then
    printf 'Refused: --ids must be a comma-separated list of sample ids.\n' >&2
    exit 64
  fi
  # The estimator and the runner refuse ids beside a limit, so only one reaches
  # them; --limit still travels as the approved count in the environment, where
  # the solver refuses an id list that does not match it.
  local -a selection
  if [[ -n "$ids" ]]; then
    local -a id_list
    IFS=',' read -ra id_list <<< "$ids"
    selection=(--ids "${id_list[@]}")
  else
    selection=(--limit "$limit")
  fi
  trials="${trials:-1}"
  if [[ ! "$trials" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Refused: --trials must be a positive integer.\n' >&2
    exit 64
  fi
  case "$split" in
    development|regression|holdout) ;;
    *)
      printf 'Refused: --split must be development, regression, or holdout.\n' >&2
      exit 64
      ;;
  esac
  if [[ ! "$approve" =~ ^\$?[0-9]+([.][0-9]{1,2})?$ ]]; then
    printf 'Cannot parse --i-approve value: %s\n' "$approve" >&2
    exit 64
  fi
  local approve_clean="${approve#\$}"
  local approved_cents
  approved_cents="$(printf '%s' "$approve_clean" | awk '{printf "%.0f", $1 * 100}')" || {
    printf 'Cannot parse --i-approve value: %s\n' "$approve" >&2
    exit 64
  }

  if ! mkdir -p "$output_dir" || [[ ! -d "$output_dir" || ! -w "$output_dir" ]]; then
    printf 'Refused: --output-dir is not a writable directory: %s\n' "$output_dir" >&2
    exit 73
  fi

  local estimate_output
  local estimated_cents=""
  if ! estimate_output="$(uv run python -m evals.inspect.cost_estimate \
      --data-root "$data_root" --stage "$stage" --provider "$provider" \
      ${model:+--model "$model"} ${effort:+--effort "$effort"} "${selection[@]}" \
      --trials "$trials" --arms "$arms" --task "$task_file" --split "$split")"; then
    printf 'The %s-stage cost estimate is unavailable. Costs are advisory.\n' "$stage" >&2
  else
    printf '%s\n' "$estimate_output"
    local estimated
    estimated="$(printf '%s' "$estimate_output" | awk -F'\\$' '{print $NF}')"
    estimated_cents="$(printf '%s' "$estimated" | awk '{printf "%.0f", $1 * 100}')"
  fi

  local -a smoke_args=()
  if ((smoke)); then smoke_args=(--smoke "$approve_clean"); fi

  CLEARSCOUT_EVAL_LIVE_APPROVED_CENTS="$approved_cents" \
    CLEARSCOUT_EVAL_ESTIMATED_CENTS="$estimated_cents" \
    CLEARSCOUT_EVAL_LIVE_PROVIDER="$provider" \
    CLEARSCOUT_EVAL_LIVE_MODEL="$model" \
    CLEARSCOUT_EVAL_LIVE_EFFORT="$effort" \
    CLEARSCOUT_EVAL_DATASET_SPLIT="$split" \
    CLEARSCOUT_EVAL_SAMPLE_LIMIT="$limit" \
    CLEARSCOUT_EVAL_SAMPLE_IDS="$ids" \
    CLEARSCOUT_EVAL_TRIALS="$trials" \
    CLEARSCOUT_EVAL_PROMPT_ARMS="$prompt_arms" \
    CLEARSCOUT_EVAL_OUTPUT_DIR="$output_dir" \
    CLEARSCOUT_EVAL_SELECTION_MANIFEST="$selection_manifest" \
    CLEARSCOUT_EVAL_SELECTION_MANIFEST_SHA256="$selection_manifest_sha256" \
    CLEARSCOUT_EVAL_SELECTION_MANIFEST_BYTES_SHA256="$selection_manifest_bytes_sha256" \
    CLEARSCOUT_EVAL_CV_VERSION="$cv_version" \
    CLEARSCOUT_EVAL_DATABASE_URL="$database_url" \
    uv run python -m evals.inspect.runner "$task_file" \
      --log-dir "$output_dir" "${selection[@]}" --split "$split" --trials "$trials" \
      --arms "$arms" ${dataset:+--dataset "$dataset"} ${smoke_args[@]+"${smoke_args[@]}"}
}

case "$SUBCOMMAND" in
  build-calibration)
    uv run python -m evals.inspect.build_calibration "$@"
    ;;
  run)
    if [[ $# -lt 1 ]]; then
      printf 'run requires a task file argument\n' >&2
      exit 64
    fi
    mkdir -p "$LOG_DIR"
    uv run python -m evals.inspect.runner "$@" --log-dir "$LOG_DIR"
    ;;
  diff)
    if [[ $# -ne 2 ]]; then
      printf 'diff requires two eval log paths\n' >&2
      exit 64
    fi
    uv run python -m evals.inspect.diff "$1" "$2"
    ;;
  --rescore)
    if [[ $# -lt 2 ]]; then
      printf -- '--rescore requires a task file and an eval log path\n' >&2
      exit 64
    fi
    uv run python -m evals.inspect.runner "$1" --rescore "$2" "${@:3}"
    ;;
  --live)
    live_run "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    printf 'Unknown subcommand: %s\n' "$SUBCOMMAND" >&2
    usage
    exit 64
    ;;
esac
