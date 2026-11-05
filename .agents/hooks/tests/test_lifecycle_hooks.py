"""Edit, session, compaction, and stop hooks behave as their clients expect."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1]
REPO = HOOKS.parents[1]
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.test"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run([*GIT, *args], cwd=repo, check=True, capture_output=True)  # noqa: S603


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """An initialized synthetic repository with one commit."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def test_migration_guard_reads_claude_serena_and_codex_payloads(
    load_hook, run_hook, git_repo: Path
) -> None:
    migration = load_hook("block-extra-migration")
    untagged = {"CLAUDE_PROJECT_DIR": str(git_repo)}
    target = "src/clearscout/infrastructure/persistence/migrations/versions/v0002.py"
    assert migration.paths_from_payload({"tool_name": "Write", "tool_input": {"file_path": target}})
    serena = {
        "tool_name": "mcp__serena__replace_symbol_body",
        "tool_input": {"relative_path": target},
    }
    assert migration.paths_from_payload(serena)
    patch = f"*** Update File: ok.py\n*** Add File: {target}\n"
    codex = migration.paths_from_payload(
        {"tool_name": "apply_patch", "tool_input": {"command": patch}}
    )
    assert codex == ["ok.py", target]
    assert run_hook("block-extra-migration", serena, env=untagged).returncode == 2
    edit_v0001 = {
        "tool_name": "Edit",
        "tool_input": {"file_path": target.replace("v0002", "v0001_schema")},
    }
    assert run_hook("block-extra-migration", edit_v0001, env=untagged).returncode == 0


def test_migration_guard_allows_serena_reads_of_the_versions_directory(run_hook) -> None:
    versions = "src/clearscout/infrastructure/persistence/migrations/versions"
    for tool in ("mcp__serena__find_symbol", "mcp__serena__search_for_pattern"):
        payload = {"tool_name": tool, "tool_input": {"relative_path": versions}}
        assert run_hook("block-extra-migration", payload).returncode == 0, tool


SERENA_READS = (
    "find_symbol",
    "search_for_pattern",
    "get_symbols_overview",
    "find_referencing_symbols",
)
SERENA_EDITS = (
    "replace_symbol_body",
    "insert_after_symbol",
    "insert_before_symbol",
    "rename_symbol",
    "safe_delete_symbol",
)


def _migration_matcher() -> re.Pattern[str]:
    settings = json.loads((REPO / ".claude/settings.json").read_text(encoding="utf-8"))
    for group in settings["hooks"]["PreToolUse"]:
        if any("block-extra-migration" in hook["command"] for hook in group["hooks"]):
            return re.compile(group["matcher"])
    raise AssertionError("block-extra-migration is not registered")


def test_mutation_predicates_agree_with_the_registered_matcher(load_hook) -> None:
    migration = load_hook("block-extra-migration")
    onboarding = load_hook("stop-check-onboarding-reads")
    matcher = _migration_matcher()
    edits = [*(f"mcp__serena__{t}" for t in SERENA_EDITS), "Write", "Edit"]
    edits.append("NotebookEdit")
    payload = {"relative_path": "src/a.py"}
    for name in [*edits, *(f"mcp__serena__{t}" for t in SERENA_READS)]:
        expected = name in edits
        assert bool(matcher.search(name)) is expected, name
        found = migration.paths_from_payload({"tool_name": name, "tool_input": payload})
        assert bool(found) is expected, name
        is_edit = name in onboarding.MUTATING_TOOLS or name.startswith(
            onboarding.MUTATING_MCP_PREFIXES
        )
        assert is_edit is expected, name


def test_codex_matcher_and_guard_take_serena_edits_under_any_server_name(load_hook) -> None:
    migration = load_hook("block-extra-migration")
    hooks = json.loads((REPO / ".codex/hooks.json").read_text(encoding="utf-8"))
    matcher = next(
        re.compile(group["matcher"])
        for group in hooks["hooks"]["PreToolUse"]
        if any("block-extra-migration" in hook["command"] for hook in group["hooks"])
    )
    payload = {"relative_path": "src/a.py"}
    for server in ("serena", "serena_clearscout"):
        for tool, expected in [
            *((t, True) for t in SERENA_EDITS),
            *((t, False) for t in SERENA_READS),
        ]:
            name = f"mcp__{server}__{tool}"
            assert bool(matcher.search(name)) is expected, name
            found = migration.paths_from_payload({"tool_name": name, "tool_input": payload})
            assert bool(found) is expected, name
    for name in ("Edit", "Write", "apply_patch"):
        assert matcher.search(name), name


def test_comment_policy_hook_reads_claude_and_serena_writes(
    load_hook, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook = load_hook("post-comment-policy-subagent")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    absolute = str(tmp_path / "a.py")
    assert hook.written_path({"tool_name": "Write", "tool_input": {"file_path": absolute}}) == (
        tmp_path / "a.py"
    )
    serena = {
        "tool_name": "mcp__serena__replace_symbol_body",
        "tool_input": {"relative_path": "a.py"},
    }
    assert hook.written_path(serena) == tmp_path / "a.py"
    for name in ("mcp__serena__find_symbol", "Bash", "NotebookEdit"):
        assert (
            hook.written_path({"tool_name": name, "tool_input": {"relative_path": "a.py"}}) is None
        )


def test_comment_policy_matcher_covers_the_tools_the_hook_reads(load_hook) -> None:
    hook = load_hook("post-comment-policy-subagent")
    settings = json.loads((REPO / ".claude/settings.json").read_text(encoding="utf-8"))
    matcher = next(
        re.compile(group["matcher"])
        for group in settings["hooks"]["PostToolUse"]
        if any("post-comment-policy-subagent" in h["command"] for h in group["hooks"])
    )
    for name in (
        "Write",
        "Edit",
        "mcp__serena__replace_symbol_body",
        "mcp__serena__insert_after_symbol",
    ):
        payload = {"tool_name": name, "tool_input": {"file_path": "/a.py", "relative_path": "a.py"}}
        assert matcher.search(name), name
        assert hook.written_path(payload) is not None, name
    assert not matcher.search("mcp__serena__find_symbol")


@pytest.mark.parametrize(
    ("agent_type", "file_path", "refused"),
    [
        ("cto-review", "src/clearscout/domain/identity.py", True),
        ("cto-review", ".claude/agent-memory-local/cto-review/MEMORY.md", False),
        ("cto-review", ".claude/agent-memory-local/../agents/cto-review.md", True),
        ("writer", "src/clearscout/domain/identity.py", False),
        ("", "src/clearscout/domain/identity.py", False),
    ],
)
def test_a_review_agent_writes_only_inside_its_memory(
    run_hook, tmp_path: Path, agent_type: str, file_path: str, refused: bool
) -> None:
    payload = {
        "tool_name": "Edit",
        "agent_type": agent_type,
        "tool_input": {"file_path": file_path},
    }
    done = run_hook("block-reviewer-writes", payload, env={"CLAUDE_PROJECT_DIR": str(tmp_path)})
    assert (done.returncode == 2) is refused
    assert ("read-only" in done.stderr) is refused


def test_reviewer_write_guard_is_registered_for_write_and_edit() -> None:
    settings = json.loads((REPO / ".claude/settings.json").read_text(encoding="utf-8"))
    matcher = next(
        re.compile(group["matcher"])
        for group in settings["hooks"]["PreToolUse"]
        if any("block-reviewer-writes" in h["command"] for h in group["hooks"])
    )
    assert matcher.search("Write") and matcher.search("Edit")
    assert not matcher.search("Bash")


def test_codex_stop_hooks_run_on_a_codex_stop_payload(run_hook, git_repo: Path) -> None:
    hooks = json.loads((REPO / ".codex/hooks.json").read_text(encoding="utf-8"))
    commands = [h["command"] for group in hooks["hooks"]["Stop"] for h in group["hooks"]]
    names = [
        Path(shlex.split(c.replace("$(git rev-parse --show-toplevel)", str(REPO)))[0]).stem
        for c in commands
    ]
    assert names == ["stop-test-collect", "stop-check-instruction-paths"]
    (git_repo / "AGENTS.md").write_text("Read `docs/missing.md` first.\n", encoding="utf-8")
    _git(git_repo, "add", "AGENTS.md")
    payload = {"hook_event_name": "Stop", "stop_hook_active": False, "cwd": str(git_repo)}
    env = {"CLAUDE_PROJECT_DIR": str(git_repo)}
    checked = run_hook("stop-check-instruction-paths", payload, cwd=git_repo, env=env)
    assert checked.returncode == 2
    assert "docs/missing.md" in checked.stderr


def test_migration_guard_stands_down_once_a_release_is_tagged(run_hook, git_repo: Path) -> None:
    """After the first tag, a schema change is a new revision, so the guard allows it."""
    target = "src/clearscout/infrastructure/persistence/migrations/versions/v0002.py"
    payload = {"tool_name": "Write", "tool_input": {"file_path": target}}
    env = {"CLAUDE_PROJECT_DIR": str(git_repo)}
    assert run_hook("block-extra-migration", payload, env=env).returncode == 2
    _git(git_repo, "tag", "v0.1.0")
    assert run_hook("block-extra-migration", payload, env=env).returncode == 0


def test_formatter_skips_agent_state_but_not_worktree_sources(load_hook) -> None:
    formatter = load_hook("format-on-write-scoped")
    assert not formatter.has_skip_segment(Path("/repo/.claude/worktrees/w/src/example.py"))
    assert formatter.has_skip_segment(Path("/repo/.claude/state/context.json"))
    assert formatter.has_skip_segment(Path("/repo/web/dist/index.js"))


def test_collector_finds_new_and_renamed_tests_only(load_hook, git_repo: Path) -> None:
    collector = load_hook("stop-test-collect")
    (git_repo / "tests").mkdir()
    for name in ("test_gone.py", "test_old.py"):
        (git_repo / "tests" / name).write_text("def test_x():\n    assert True\n", encoding="utf-8")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-qm", "tests")
    (git_repo / "tests/test_gone.py").unlink()
    _git(git_repo, "mv", "tests/test_old.py", "tests/test_moved.py")
    (git_repo / "tests/test_data.json").write_text("{}", encoding="utf-8")
    (git_repo / "tests/test_with space.py").write_text("", encoding="utf-8")
    assert sorted(collector.changed_test_files(git_repo)) == [
        "tests/test_moved.py",
        "tests/test_with space.py",
    ]


def test_collect_only_repeats_no_addopts_flag(load_hook, monkeypatch, tmp_path: Path) -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    flags = set(shlex.split(addopts) if isinstance(addopts, str) else addopts)
    collector = load_hook("stop-test-collect")
    seen: list[list[str]] = []
    monkeypatch.setattr(collector, "run", lambda args, *_: (seen.append(args), (0, ""))[1])
    collector.collection_result(tmp_path, ["tests/test_x.py"])
    assert seen
    assert not flags & set(seen[0])


def test_session_context_names_the_checkout_and_branch(run_hook, git_repo: Path) -> None:
    done = run_hook("session-context", {"cwd": str(git_repo), "source": "startup"})
    context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
    assert f"repository/worktree: {git_repo.resolve()}" in context
    assert "working tree: clean" in context
    assert "branch: " in context


def test_precompact_writes_a_snapshot_and_session_context_points_at_it(
    run_hook, git_repo: Path
) -> None:
    transcript = git_repo.parent / "transcript.jsonl"
    entry = {"type": "user", "message": {"role": "user", "content": "Fix the Acme importer"}}
    transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    payload = {"cwd": str(git_repo), "session_id": "s1", "transcript_path": str(transcript)}
    assert run_hook("precompact-snapshot", payload).returncode == 0

    snapshots = list((git_repo / ".claude/state").glob("precompact-s1-*.md"))
    assert len(snapshots) == 1
    assert "Fix the Acme importer" in snapshots[0].read_text(encoding="utf-8")
    done = run_hook("session-context", {"cwd": str(git_repo), "source": "compact"})
    assert "pre-compaction recovery:" in done.stdout


def test_instruction_paths_report_a_missing_path(run_hook, git_repo: Path) -> None:
    (git_repo / "docs").mkdir()
    (git_repo / "docs/guide.md").write_text("x\n", encoding="utf-8")
    (git_repo / "AGENTS.md").write_text(
        "Read `docs/guide.md` and `docs/gone.md`.\n", encoding="utf-8"
    )
    _git(git_repo, "add", ".")
    env = {"CLAUDE_PROJECT_DIR": str(git_repo)}
    done = run_hook("stop-check-instruction-paths", {}, env=env)
    assert done.returncode == 2
    assert "docs/gone.md" in done.stderr
    assert "docs/guide.md" not in done.stderr


def test_instruction_paths_cover_roles_skills_and_agents(run_hook, git_repo: Path) -> None:
    files = (".agents/roles/x.md", ".agents/skills/y/SKILL.md", ".claude/agents/z.md")
    for name in files:
        (git_repo / name).parent.mkdir(parents=True, exist_ok=True)
        (git_repo / name).write_text(f"Read `docs/{Path(name).stem}-gone.md`.\n", "utf-8")
    _git(git_repo, "add", ".")
    done = run_hook("stop-check-instruction-paths", {}, env={"CLAUDE_PROJECT_DIR": str(git_repo)})
    assert done.returncode == 2
    for missing in ("docs/x-gone.md", "docs/SKILL-gone.md", "docs/z-gone.md"):
        assert missing in done.stderr


def _transcript(tmp_path: Path, blocks: list[dict]) -> Path:
    path = tmp_path / "t.jsonl"
    lines = [json.dumps({"type": "assistant", "message": {"content": [b]}}) for b in blocks]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_onboarding_nudge_fires_only_without_a_first_read(run_hook, tmp_path: Path) -> None:
    edits = [{"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}}] * 3
    unread = _transcript(tmp_path, edits)
    done = run_hook("stop-check-onboarding-reads", {"transcript_path": str(unread)})
    assert "systemMessage" in done.stdout

    assert "AGENTS.md" not in json.loads(done.stdout)["systemMessage"]

    read = {"type": "tool_use", "name": "Read", "input": {"file_path": "/x/README.md"}}
    informed = _transcript(tmp_path, [read, *edits])
    done = run_hook("stop-check-onboarding-reads", {"transcript_path": str(informed)})
    assert done.stdout.strip() == ""


def test_onboarding_nudge_fires_once_per_session(run_hook, tmp_path: Path) -> None:
    edits = [{"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}}] * 3
    payload = {"session_id": "s1", "transcript_path": str(_transcript(tmp_path, edits))}
    env = {"TMPDIR": str(tmp_path)}
    first = run_hook("stop-check-onboarding-reads", payload, env=env)
    assert "systemMessage" in first.stdout
    again = run_hook("stop-check-onboarding-reads", payload, env=env)
    assert again.stdout.strip() == ""
    other = run_hook("stop-check-onboarding-reads", {**payload, "session_id": "s2"}, env=env)
    assert "systemMessage" in other.stdout


def _registered_commands() -> list[str]:
    settings = json.loads((REPO / ".claude/settings.json").read_text(encoding="utf-8"))
    return [
        hook["command"]
        for groups in settings["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]


def test_every_hook_is_registered_and_executable() -> None:
    prefix = '"$CLAUDE_PROJECT_DIR"/.agents/hooks/'
    commands = _registered_commands()
    assert all(command.startswith(prefix) for command in commands)
    registered = {command.removeprefix(prefix) for command in commands}
    hooks = [*HOOKS.glob("*.py"), *HOOKS.glob("*.sh")]
    assert registered == {path.name for path in hooks}
    for name in registered:
        assert os.access(HOOKS / name, os.X_OK), name
