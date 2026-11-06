"""The Bash guards refuse their target shapes and leave ordinary commands alone."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

RunHook = Callable[..., subprocess.CompletedProcess[str]]
PYTEST = "pyt" + "est"


def _bash(command: str, agent_id: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"tool_name": "Bash", "tool_input": {"command": command}}
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A synthetic checkout with a few tracked-looking files."""
    for name in ("AGENTS.md", "README.md", "web/src/app.css", "tests/test_x.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    return tmp_path


class TestCatHeredoc:
    def _code(self, run_hook: RunHook, repo: Path, command: str) -> int:
        env = {"CLAUDE_PROJECT_DIR": str(repo), "TMPDIR": str(repo / "scratch")}
        return run_hook("block-cat-heredoc", _bash(command), cwd=repo, env=env).returncode

    def test_a_python_heredoc_editing_a_repo_file_is_refused(
        self, run_hook: RunHook, repo: Path
    ) -> None:
        command = (
            "python3 - <<'PY'\nfrom pathlib import Path\n"
            "p = Path('tests/test_x.py')\np.write_text(p.read_text().replace('a', 'b'))\nPY"
        )
        assert self._code(run_hook, repo, command) == 2

    def test_a_repo_path_in_a_node_heredoc_is_refused(self, run_hook: RunHook, repo: Path) -> None:
        command = "node <<'JS'\nrequire('fs').writeFileSync('./web/src/a.ts', '')\nJS"
        assert self._code(run_hook, repo, command) == 2

    def test_a_cat_redirect_into_the_repo_is_refused(self, run_hook: RunHook, repo: Path) -> None:
        command = "cat >> web/src/app.css <<'CSS'\n.x {}\nCSS"
        assert self._code(run_hook, repo, command) == 2

    @pytest.mark.parametrize(
        "command",
        [
            "python3 - <<'PY'\nfrom pathlib import Path\nPath('/tmp/x.json').write_text('{}')\nPY",
            "cat > \"$TMPDIR/usage.py\" <<'PY'\nprint(1)\nPY",
            "python3 - <<'PY'\nprint(2 + 2)\nPY",
            "cat README.md",
        ],
    )
    def test_scratch_and_read_only_shapes_are_allowed(
        self, run_hook: RunHook, repo: Path, command: str
    ) -> None:
        assert self._code(run_hook, repo, command) == 0

    @pytest.mark.parametrize("memory_dir", ["agent-memory", "agent-memory-local"])
    def test_a_heredoc_into_agent_memory_is_allowed(
        self, run_hook: RunHook, repo: Path, memory_dir: str
    ) -> None:
        target = repo / ".claude" / memory_dir / "cto-review" / "MEMORY.md"
        command = f"cat > {target} <<'MD'\n- note\nMD"
        assert self._code(run_hook, repo, command) == 0

    def test_a_heredoc_into_another_claude_dir_is_refused(
        self, run_hook: RunHook, repo: Path
    ) -> None:
        command = "cat > .claude/agent-memory-other/MEMORY.md <<'MD'\n- note\nMD"
        assert self._code(run_hook, repo, command) == 2


def test_without_a_project_dir_the_checkout_holding_the_hook_is_guarded(
    run_hook: RunHook, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    checkout = Path(__file__).resolve().parents[3]
    write = run_hook("block-cat-heredoc", _bash(f"cat > {checkout}/new.md <<'MD'\nx\nMD"))
    assert write.returncode == 2


@pytest.mark.parametrize(
    ("blocked", "command"),
    [
        (False, "grep -rn 'Foo' tests/ src/ | wc -l"),
        (False, "grep -n 'vocab' scripts/gate.sh | head -3"),
        (False, "cat scripts/gate.sh | head -40"),
        (False, f"uv run {PYTEST} > /tmp/o.txt 2>&1"),
        (False, "scripts/gate.sh all | tee /tmp/g.log"),
        (False, "just pytest-log x tests/a.py"),
        (True, f"uv run {PYTEST} tests/ | grep FAIL"),
        (True, "npm --prefix web run test | tail -5"),
        (True, "scripts/gate.sh all | grep -i fail"),
        (True, "scripts/eval.sh run | head -20"),
        (True, f"cd web && uv run {PYTEST} | grep E"),
        (True, f"uv run --no-sync {PYTEST} tests/loop | tail -5"),
        (True, "just pytest-log x tests/a.py | grep exit"),
    ],
)
def test_piped_test_output(run_hook: RunHook, blocked: bool, command: str) -> None:
    assert run_hook("block-piped-test-output", _bash(command)).returncode == (2 if blocked else 0)


@pytest.mark.parametrize(
    ("blocked", "command"),
    [
        (True, "cat .env"),
        (True, "grep KEY .env.local"),
        (True, "source .env"),
        (True, "envsubst < .env"),
        (True, "head -n1 ~/.aws/credentials"),
        (False, "test -f .env"),
        (False, "ls -la .env"),
        (False, "cat README.md"),
    ],
)
def test_credential_reads(run_hook: RunHook, blocked: bool, command: str) -> None:
    assert run_hook("block-credential-reads", _bash(command)).returncode == (2 if blocked else 0)


GIT_CASES: list[tuple[str | None, str, str]] = [
    ("deny", "git stash", ""),
    ("deny", "git stash", "sub-1"),
    ("deny", "git stash -u", ""),
    ("deny", "git stash pop", ""),
    (None, "git stash list", ""),
    (None, "git stash show", ""),
    ("deny", "git -C /tmp/x stash", ""),
    ("deny", "cd /tmp && git stash", ""),
    ("deny", "bash -c 'git stash pop'", ""),
    ("deny", "env A=1 git stash", ""),
    ("deny", "/usr/bin/git stash", ""),
    ("deny", 'echo "$(git stash)"', ""),
    ("deny", "(git stash)", ""),
    ("deny", "{ git stash; }", ""),
    ("deny", "xargs git stash", ""),
    ("deny", "if true; then git stash; fi", ""),
    ("deny", "timeout 5 git stash", ""),
    ("deny", "! git stash", ""),
    ("deny", "git checkout -- file.py", "sub-1"),
    ("deny", "git restore file.py", "sub-1"),
    ("deny", "git reset --hard HEAD~1", "sub-1"),
    ("deny", "git clean -fd", "sub-1"),
    ("deny", "git switch main", "sub-1"),
    ("deny", "git worktree remove x", "sub-1"),
    ("deny", "(git checkout -- src/x.py)", "sub-1"),
    (None, "git checkout main", ""),
    (None, "git status", "sub-1"),
    (None, "git diff", "sub-1"),
    (None, "git log --oneline -5", "sub-1"),
    (None, "git worktree list", "sub-1"),
    (None, "git log --grep stash", ""),
    (None, "rg stash src", ""),
    ("deny", "scripts/gate.sh change", "sub-1"),
    ("deny", "bash scripts/gate.sh py", "sub-1"),
    ("deny", "GATE_FIX=0 scripts/gate.sh package", "sub-1"),
    ("deny", "just gate py", "sub-1"),
    ("deny", "mkdir -p /tmp/x && scripts/gate.sh web > /tmp/x/g.log 2>&1", "sub-1"),
    (None, "scripts/gate.sh change", ""),
    (None, "just pytest-log tests_x tests/test_x.py", "sub-1"),
    (None, "rg FAILED /tmp/x/gate.log", "sub-1"),
]


@pytest.mark.parametrize(("expected", "command", "agent_id"), GIT_CASES)
def test_shared_checkout_git(
    run_hook: RunHook, expected: str | None, command: str, agent_id: str
) -> None:
    done = run_hook("block-shared-checkout-git", _bash(command, agent_id))
    verdict = None
    if done.stdout.strip():
        verdict = json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"]
    assert verdict == expected


def test_shared_checkout_git_ignores_other_tools(run_hook: RunHook) -> None:
    other = run_hook("block-shared-checkout-git", {"tool_name": "Write", "tool_input": {}})
    assert other.stdout.strip() == ""
    assert other.returncode == 0
