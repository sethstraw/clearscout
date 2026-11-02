from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
from _pytest.capture import CaptureFixture
from scripts.audit_public_data import EVAL_EMPLOYERS, SYNTHETIC_EMPLOYERS
from scripts.audit_public_data import main as audit_public_data
from scripts.check_markdown_links import main as check_markdown_links
from scripts.check_release_tag import main as check_release_tag
from scripts.verify_distribution import DistributionError, verify_archive

from tests.support.suite import REPO_ROOT

ROOT = REPO_ROOT
INDEX = (
    '<link rel="stylesheet" href="/assets/app.css">'
    '<script type="module" src="/assets/app.js"></script>'
)


def _write_archive(path: Path, *, include_assets: bool) -> None:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("tailorcv/web/index.html", INDEX)
            if include_assets:
                archive.writestr("tailorcv/web/assets/app.css", "body{}")
                archive.writestr("tailorcv/web/assets/app.js", "")
        return
    with tarfile.open(path, "w:gz") as archive:
        contents = {"tailorcv-0.1.0/web/dist/index.html": INDEX}
        if include_assets:
            contents.update(
                {
                    "tailorcv-0.1.0/web/dist/assets/app.css": "body{}",
                    "tailorcv-0.1.0/web/dist/assets/app.js": "",
                }
            )
        for name, content in contents.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


@pytest.mark.parametrize("filename", ["tailorcv-0.1.0-py3-none-any.whl", "tailorcv-0.1.0.tar.gz"])
def test_distribution_verifier_accepts_frontend_and_referenced_assets(
    tmp_path: Path, filename: str
) -> None:
    archive = tmp_path / filename
    _write_archive(archive, include_assets=True)

    verify_archive(archive)


def test_distribution_verifier_rejects_missing_referenced_asset(tmp_path: Path) -> None:
    archive = tmp_path / "tailorcv-0.1.0-py3-none-any.whl"
    _write_archive(archive, include_assets=False)

    with pytest.raises(DistributionError, match="assets/app.css"):
        verify_archive(archive)


def test_generated_spa_is_ignored_and_forced_into_distributions() -> None:
    git = shutil.which("git")
    assert git is not None
    ignored = subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
        [git, "check-ignore", "--no-index", "--quiet", "web/dist/index.html"],
        cwd=ROOT,
        check=False,
    )
    assert ignored.returncode == 0

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = config["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"]["force-include"]["web/dist"] == "tailorcv/web"
    assert targets["sdist"]["force-include"]["web/dist"] == "web/dist"


def test_public_agent_instructions_are_tracked_and_shipped() -> None:
    git = shutil.which("git")
    assert git is not None
    ignored = subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
        [git, "check-ignore", "--quiet", "AGENTS.md"],
        cwd=ROOT,
        check=False,
    )
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert ignored.returncode == 1
    assert "/AGENTS.md" in config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]


def test_public_data_audit_scans_ignored_generated_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
        [git, "init", "--quiet"], cwd=tmp_path, check=True
    )
    (tmp_path / ".gitignore").write_text("web/dist/\n", encoding="utf-8")
    (tmp_path / "safe.txt").write_text("safe\n", encoding="utf-8")
    asset = tmp_path / "web" / "dist" / "assets" / "app.js"
    asset.parent.mkdir(parents=True)
    machine_path = "/" + "Users/private-user/source"
    asset.write_text(
        f'const source = "{machine_path}"; const key = "sk-test-abcdefghijklmno";\n',
        encoding="utf-8",
    )
    subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
        [git, "add", ".gitignore", "safe.txt"], cwd=tmp_path, check=True
    )
    monkeypatch.chdir(tmp_path)

    assert audit_public_data() == 1
    output = capsys.readouterr().out
    assert "web/dist/assets/app.js: machine-specific absolute path" in output
    assert "web/dist/assets/app.js: key-shaped value" in output


def _fixture_repository(root: Path, body: str, at: str = "tests/loop/test_boards.py") -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - resolved executable, fixed arguments
        [git, "init", "--quiet"], cwd=root, check=True
    )
    fixture = root / at
    fixture.parent.mkdir(parents=True)
    fixture.write_text(body, encoding="utf-8")


def test_public_data_audit_names_the_file_and_the_employer_a_fixture_may_not_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    # The planted name is assembled so this file does not itself trip the scan.
    planted = "Quantoria" + " Biosciences"
    tenant = "quantoria" + "-bio"
    _fixture_repository(
        tmp_path,
        "listing = SourceListing" + f'("{planted}", "Engineer", BOARD)\n'
        f'posting = {{"company": "{planted}"}}\n'
        'BOARD = "https://jobs.lever.co/' + f'{tenant}/7d811a2b-31b8-453f-94db-ff456ffd0376"\n',
    )
    monkeypatch.chdir(tmp_path)

    assert audit_public_data() == 1
    output = capsys.readouterr().out
    assert f'tests/loop/test_boards.py: employer name "{planted}" is not synthetic' in output
    assert f'tests/loop/test_boards.py: employer name "{tenant}" is not synthetic' in output
    assert f"use one of {', '.join(SYNTHETIC_EMPLOYERS)}" in output


def test_public_data_audit_reads_a_board_platform_as_capability_rather_than_an_employer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    _fixture_repository(
        tmp_path,
        'GREENHOUSE = "https://boards.greenhouse.io/acme/jobs/1"\n'
        'WORKDAY = "https://globex.wd5.myworkdayjobs.com/en-US/Careers"\n'
        'ORACLE = "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1"\n'
        'RECRUITEE = "https://initech.recruitee.com/o/engineer"\n'
        'ASHBY = "https://jobs.ashbyhq.com/globex"\n'
        'DOVER = "https://app.dover.io/dover/careers/7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"\n'
        'WORKABLE = "https://www.workable.com/api/accounts/100200"\n'
        'listing = SourceListing("Northwind Analytics", "Engineer", GREENHOUSE)\n',
    )
    monkeypatch.chdir(tmp_path)

    assert audit_public_data() == 0
    assert "is not synthetic" not in capsys.readouterr().out


def test_public_data_audit_holds_an_eval_case_to_the_employers_that_corpus_uses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """Eval cases ship in the sdist, so a real employer named in one is published."""
    # The planted name is assembled so this file does not itself trip the scan.
    planted = "Quantoria" + " Biosciences"
    _fixture_repository(tmp_path, f'employer = "{planted}"\n', at="evals/cases/fit.toml")
    monkeypatch.chdir(tmp_path)

    assert audit_public_data() == 1
    output = capsys.readouterr().out
    assert f'evals/cases/fit.toml: employer name "{planted}" is not synthetic' in output
    assert f"use one of {', '.join(EVAL_EMPLOYERS)}" in output


@pytest.mark.parametrize(
    "address",
    [
        "https://globex.wd5.myworkdayjobs.com/en-US/Careers",
        "https://wd1.myworkdaysite.com/en-US/recruiting/acme/External",
    ],
)
def test_public_data_audit_accepts_synthetic_workday_tenants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
    address: str,
) -> None:
    _fixture_repository(tmp_path, f'BOARD = "{address}"\n')
    monkeypatch.chdir(tmp_path)

    assert audit_public_data() == 0
    assert "is not synthetic" not in capsys.readouterr().out


def test_public_data_audit_reports_the_explicit_workday_tenant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    tenant = "quantoria" + "-bio"
    _fixture_repository(
        tmp_path,
        f'BOARD = "https://wd1.myworkdaysite.com/en-US/recruiting/{tenant}/External"\n',
    )
    monkeypatch.chdir(tmp_path)

    assert audit_public_data() == 1
    output = capsys.readouterr().out
    assert f'employer name "{tenant}" is not synthetic' in output
    assert 'employer name "wd1"' not in output


def test_markdown_link_check_reads_its_own_repository_from_any_directory(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "check_markdown_links.py", repository / "scripts")
    (repository / "README.md").write_text("Read [this](gone.md).\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = subprocess.run(  # noqa: S603 - fixed arguments, current interpreter
        [sys.executable, str(repository / "scripts" / "check_markdown_links.py")],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "README.md:1: missing gone.md" in result.stdout


def test_markdown_link_check_skips_local_tool_state(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """Gitignored tool directories are not documentation, so the check skips them.

    A fresh clone has neither directory, so scanning them would make the answer
    depend on which machine ran the check.
    """
    (tmp_path / "README.md").write_text("Read `scripts/real.py` for it.\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "real.py").write_text("", encoding="utf-8")
    for directory in (".serena/memories", ".claude/state"):
        stale = tmp_path / directory
        stale.mkdir(parents=True)
        (stale / "core.md").write_text("Read `tests/gone.py` for it.\n", encoding="utf-8")
    (tmp_path / ".claude" / "rules").mkdir(parents=True)
    for overlay in ("CLAUDE.local.md", ".claude/rules/local-private.md"):
        (tmp_path / overlay).write_text("Read `tests/gone.py` for it.\n", encoding="utf-8")

    assert check_markdown_links(tmp_path) == 0
    assert "1 files" in capsys.readouterr().out


def test_markdown_link_check_reads_the_shared_agent_setup(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """Tracked agent rules are documentation, so a stale path in one fails."""
    rules = tmp_path / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "edits.md").write_text("Read `tests/gone.py` for it.\n", encoding="utf-8")

    assert check_markdown_links(tmp_path) == 1
    assert ".claude/rules/edits.md:1: missing tests/gone.py" in capsys.readouterr().out


def test_markdown_link_check_rejects_a_missing_heading_anchor(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    (tmp_path / "README.md").write_text("# Install a tagged release\n", encoding="utf-8")
    (tmp_path / "guide.md").write_text(
        "Read the [release instructions](README.md#release-state).\n", encoding="utf-8"
    )

    assert check_markdown_links(tmp_path) == 1
    assert "guide.md:1: missing README.md#release-state" in capsys.readouterr().out


def test_markdown_link_check_accepts_github_heading_anchors(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Install a `tagged` release\n\n# Install a `tagged` release\n", encoding="utf-8"
    )
    (tmp_path / "guide.md").write_text(
        "Read the [first](README.md#install-a-tagged-release) and "
        "[second](README.md#install-a-tagged-release-1).\n",
        encoding="utf-8",
    )

    assert check_markdown_links(tmp_path) == 0


def test_ci_builds_the_spa_before_the_python_distributions() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    check = workflow.split("  check:\n", maxsplit=1)[1].split("  floor:\n", maxsplit=1)[0]
    floor = workflow.split("  floor:\n", maxsplit=1)[1].split("  package:\n", maxsplit=1)[0]
    package = workflow.split("  package:\n", maxsplit=1)[1].split("  install:\n", maxsplit=1)[0]

    assert "bash scripts/gate.sh py" in check
    assert "bash scripts/gate.sh generated" in check
    assert "bash scripts/gate.sh security" in check
    assert "bash scripts/gate.sh package" in package
    for job in (floor,):
        assert job.index("npm --prefix web run build") < job.index("uv sync")
    assert "actions/setup-node@" in package
    assert package.index("npm ci --prefix web") < package.index("bash scripts/gate.sh package")


def test_tag_release_attaches_distributions_and_checksums() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'tags: ["v*"]' in workflow
    assert "scripts/check_release_tag.py" in workflow
    assert workflow.index("scripts/check_release_tag.py") < workflow.index(
        "bash scripts/gate.sh all"
    )
    assert "bash scripts/gate.sh all" in workflow
    assert "sha256sum" in workflow
    assert "gh release create" in workflow


def test_tag_release_attests_the_distributions_it_publishes() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "actions/attest-build-provenance@" in workflow
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert workflow.index("actions/attest-build-provenance@") < workflow.index("gh release create")


def test_release_tag_must_match_the_package_version() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]

    assert check_release_tag([f"v{version}"]) == 0
    assert check_release_tag(["v9.9.9"]) == 1


def test_gate_verifies_generated_artifacts_without_rewriting_tracked_files() -> None:
    gate = (ROOT / "scripts/gate.sh").read_text(encoding="utf-8")

    generated = gate.split("run_generated() {", maxsplit=1)[1].split("\n}", maxsplit=1)[0]
    assert "tmpdir" in generated
    assert "diff -u" in gate
    assert "--write-notices THIRD_PARTY_NOTICES.md" not in generated
    assert "--write-license-texts THIRD_PARTY_LICENSES.txt" not in generated


def test_every_gate_step_runs_at_some_scope_a_hook_or_ci_reaches() -> None:
    """The cheap default cannot quietly drop a check nothing else runs.

    `change` is what an edit runs and `push` carries the expensive halves. A
    step in neither would be a check that exists and never executes.
    """
    gate = (ROOT / "scripts/gate.sh").read_text(encoding="utf-8")
    dispatch = gate.split('case "$SCOPE" in', maxsplit=1)[1]
    change = dispatch.split("  change)", maxsplit=1)[1].split(";;", maxsplit=1)[0]
    push = dispatch.split("  push)", maxsplit=1)[1].split(";;", maxsplit=1)[0]
    everything = dispatch.split("  all)", maxsplit=1)[1].split(";;", maxsplit=1)[0]

    runners = {line.strip() for line in everything.splitlines() if line.strip().startswith("run_")}
    covered = {line.strip() for line in (change + push).splitlines() if line.strip()}
    assert runners == covered

    hook = (ROOT / ".githooks/pre-push").read_text(encoding="utf-8")
    assert "scripts/gate.sh push" in hook
    assert 'cd "$(git rev-parse --show-toplevel)"' in hook


def test_gate_scopes_cover_release_security_package_and_workflows() -> None:
    gate = (ROOT / "scripts/gate.sh").read_text(encoding="utf-8")

    assert "run_security()" in gate
    assert "run_package()" in gate
    assert "run_workflows()" in gate
    assert "uv run --locked" in gate
    assert "uv build" in gate
    assert "check-wheel-contents==0.6.3" in gate
    assert "npx" not in gate
    assert "npm --prefix web run typecheck" in gate
