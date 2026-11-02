from __future__ import annotations

from pathlib import Path

import pytest
from scripts.check_comment_policy import file_violations, violations
from scripts.check_comment_policy import main as check_comment_policy


def test_comment_policy_reports_each_kind_of_violation(tmp_path: Path) -> None:
    source = tmp_path / "src.py"
    source.write_text(
        "value = 1\n"
        "# TODO remove this\n"
        "answer = 2\n"
        "# one\n# two\n# three\n# four\n"
        "final = 3\n"
        "# The cache is warmed at startup. Because a cold read blocks the first\n"
        "# request, the warm-up runs there.\n",
        encoding="utf-8",
    )

    assert file_violations(source) == [
        f"{source.as_posix()}:2: TODO/FIXME/HACK lacks an issue or owner",
        f"{source.as_posix()}:4: comment block has 4 lines",
        f"{source.as_posix()}:9: a sentence opens as a justification",
    ]


def test_comment_policy_allows_a_justification_clause_inside_a_sentence(tmp_path: Path) -> None:
    """A mid-sentence clause states a fact and only a sentence-initial one argues."""
    source = tmp_path / "src.py"
    source.write_text(
        "# Use the final URL because vanity domains often redirect to the board.\n"
        "# Wrapped prose keeps its second half readable\n"
        "# rather than being read as a new sentence.\n"
        "value = 1\n",
        encoding="utf-8",
    )

    assert file_violations(source) == []


def test_comment_policy_bounds_comments_but_not_documented_interfaces(tmp_path: Path) -> None:
    header = tmp_path / "header.py"
    header.write_text(
        "# Settings for recording the replay cassette.\n"
        "#\n"
        "# Everything here describes an invented candidate, and request bodies are\n"
        "# matched by digest on replay.\n"
        "value = 1\n",
        encoding="utf-8",
    )
    module = tmp_path / "module.ts"
    module.write_text(
        "const first = 1;\n"
        "/**\n"
        " * What this answers, in one place.\n"
        " *\n"
        " * The vocabulary lives beside it.\n"
        " * The server owns the rest.\n"
        " */\n"
        "const second = 2;\n"
        "// one\n// two\n// three\n// four\n"
        "const third = 3;\n",
        encoding="utf-8",
    )

    assert file_violations(header) == []
    assert file_violations(module) == [f"{module.as_posix()}:9: comment block has 4 lines"]


def test_comment_policy_does_not_read_python_unpacking_as_a_comment(tmp_path: Path) -> None:
    """A leading asterisk continues a C-family block comment and unpacks a Python iterable."""
    source = tmp_path / "src.py"
    source.write_text(
        "values = (\n    *first,\n    *second,\n    *third,\n    *fourth,\n)\n",
        encoding="utf-8",
    )

    assert file_violations(source) == []


def test_comment_policy_flags_a_docstring_longer_than_its_code(tmp_path: Path) -> None:
    source = tmp_path / "widen.py"
    source.write_text(
        "def widen(value: int) -> int:\n"
        '    """Return the widened value.\n'
        "\n"
        "    The ledger column holds an integer.\n"
        '    """\n'
        "    return value * 2\n",
        encoding="utf-8",
    )

    assert file_violations(source) == [
        f"{source.as_posix()}:2: docstring has 2 lines and the code beneath it has 1",
    ]


def test_comment_policy_allows_a_docstring_shorter_than_its_code(tmp_path: Path) -> None:
    """A module docstring is exempt, and a callable's docstring is bounded by its body."""
    source = tmp_path / "widen.py"
    source.write_text(
        '"""Widen ledger values.\n'
        "\n"
        "This module carries one helper.\n"
        '"""\n'
        "\n"
        "\n"
        "def widen(value: int) -> int:\n"
        '    """Return the widened value.\n'
        "\n"
        "    The ledger column holds an integer.\n"
        '    """\n'
        "    doubled = value * 2\n"
        "    return doubled\n",
        encoding="utf-8",
    )

    assert file_violations(source) == []


def test_comment_policy_allows_a_long_docstring_on_a_declaration_only_class(
    tmp_path: Path,
) -> None:
    """Declared members carry no logic, so their line count bounds nothing."""
    source = tmp_path / "kind.py"
    source.write_text(
        "class Kind(StrEnum):\n"
        '    """What happened to a piece of the record.\n'
        "\n"
        "    PRESERVED is text copied out of the master CV unchanged.\n"
        "    REFRAMED is text the drafter rewrote.\n"
        "    DROPPED is text the page has no room for.\n"
        '    """\n'
        '    PRESERVED = "preserved"\n'
        '    REFRAMED = "reframed"\n',
        encoding="utf-8",
    )

    assert file_violations(source) == []


def test_comment_policy_allows_a_long_docstring_on_a_route_handler(tmp_path: Path) -> None:
    """A route handler's docstring is published as the operation's description."""
    source = tmp_path / "routes.py"
    source.write_text(
        '@router.get("/budget")\n'
        "def read_budget() -> int:\n"
        '    """Report the retry budget.\n'
        "\n"
        "    This is a free read.\n"
        "    It is never scoped to one workspace.\n"
        '    """\n'
        "    return 3\n",
        encoding="utf-8",
    )

    assert file_violations(source) == []


def test_comment_policy_flags_a_long_docstring_on_a_class_with_methods(tmp_path: Path) -> None:
    source = tmp_path / "ledger.py"
    source.write_text(
        "class Ledger:\n"
        '    """Hold the widened values.\n'
        "\n"
        "    The column is an integer.\n"
        "    The caller owns the session.\n"
        '    """\n'
        "    def widen(self, value: int) -> int:\n"
        "        return value * 2\n",
        encoding="utf-8",
    )

    assert file_violations(source) == [
        f"{source.as_posix()}:2: docstring has 3 lines and the code beneath it has 2",
    ]


@pytest.mark.parametrize(
    ("comment", "rule"),
    [
        ("# Decision 1 fixes the retry budget.\n", "comment names a review code 'Decision 1'"),
        ("# D2 fixes the retry budget.\n", "comment names a review code 'D2'"),
        ("# Defect 8 fixes the retry budget.\n", "comment names a review code 'Defect 8'"),
        ("# The retry budget lives in conftest.py.\n", "comment names another file 'conftest.py'"),
        ("# See docs/retries.md for the budget.\n", "comment names another file 'See docs/'"),
        (
            "# See the design doc for the budget.\n",
            "comment names another file 'See the design doc'",
        ),
        ("# This used to be unbounded.\n", "comment narrates a change 'This used to'"),
        ("# The budget was changed to three retries.\n", "comment narrates a change 'was changed'"),
        (
            "# Three retries instead of the old unbounded loop.\n",
            "comment narrates a change 'instead of the old'",
        ),
    ],
)
def test_comment_policy_flags_a_wording_rule(tmp_path: Path, comment: str, rule: str) -> None:
    source = tmp_path / "budget.py"
    source.write_text(f"value = 1\n{comment}answer = 2\n", encoding="utf-8")

    assert file_violations(source) == [f"{source.as_posix()}:2: {rule}"]


@pytest.mark.parametrize(
    "comment",
    [
        "# The retry budget is three attempts.\n",
        "# A refused host yields no body at all.\n",
        "# Knowledge of the board shape stays in the mapper.\n",
        "# The budget is now three retries.\n",
        "# A refused host no longer holds the lease.\n",
        "# A fact previously observed for this posting is recovered.\n",
        "# The stored facts used to translate one result.\n",
        "# D203 selects a docstring convention.\n",
        "# The run writes Retry_Budget_Report.md beside its inputs.\n",
    ],
)
def test_comment_policy_allows_a_comment_that_states_a_fact(tmp_path: Path, comment: str) -> None:
    source = tmp_path / "budget.py"
    source.write_text(f"value = 1\n{comment}answer = 2\n", encoding="utf-8")

    assert file_violations(source) == []


def test_comment_policy_allows_a_usage_header_that_names_its_own_file(tmp_path: Path) -> None:
    source = tmp_path / "budget.py"
    source.write_text(
        '"""Report the retry budget.\n\nUsage: python scripts/budget.py --once\n"""\nvalue = 1\n',
        encoding="utf-8",
    )

    assert file_violations(source) == []


def test_comment_policy_reads_a_path_in_configuration_as_data(tmp_path: Path) -> None:
    """A path in a configuration file is a value the file needs, not a pointer."""
    source = tmp_path / "pyproject.toml"
    source.write_text("# The wheel carries src/tailorcv/prompts.py.\nvalue = 1\n", encoding="utf-8")

    assert file_violations(source) == []


def test_comment_policy_applies_the_wording_rules_to_a_docstring(tmp_path: Path) -> None:
    source = tmp_path / "budget.py"
    source.write_text(
        "def budget() -> int:\n"
        '    """Return the budget Rule 5 sets."""\n'
        "    attempts = 3\n"
        "    return attempts\n",
        encoding="utf-8",
    )

    assert file_violations(source) == [
        f"{source.as_posix()}:2: docstring names a review code 'Rule 5'",
    ]


def test_comment_policy_checks_only_the_named_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    named = tmp_path / "named.py"
    named.write_text("# This used to grow.\nvalue = 1\n", encoding="utf-8")
    other = tmp_path / "other.py"
    other.write_text("# The budget was changed to three retries.\nvalue = 1\n", encoding="utf-8")
    clean = tmp_path / "clean.py"
    clean.write_text("# The retry budget is three attempts.\nvalue = 1\n", encoding="utf-8")

    assert check_comment_policy(["--files", str(named)]) == 1
    reported = capsys.readouterr().out
    assert f"{named.as_posix()}:1: comment narrates a change 'This used to'" in reported
    assert other.as_posix() not in reported
    assert check_comment_policy(["--files", str(clean)]) == 0


HASH_BLOCK = "# one\n# two\n# three\n# four\n"
HASH_FACT = "# The retry budget is three attempts.\n"
SLASH_BLOCK = "// one\n// two\n// three\n// four\n"
SLASH_FACT = "// The retry budget is three attempts.\n"
HTML_BLOCK = "<!--\none\ntwo\nthree\nfour\n-->\n"
HTML_FACT = "<!-- The retry budget is three attempts. -->\n"


@pytest.mark.parametrize(
    ("name", "head", "block", "fact"),
    [
        ("module.js", "const a = 1;\n", SLASH_BLOCK, SLASH_FACT),
        ("module.cjs", "const a = 1;\n", SLASH_BLOCK, SLASH_FACT),
        ("module.mjs", "const a = 1;\n", SLASH_BLOCK, SLASH_FACT),
        ("justfile", "set shell := ['bash']\n", HASH_BLOCK, HASH_FACT),
        (".gitignore", "*.pyc\n", HASH_BLOCK, HASH_FACT),
        (".prettierignore", "dist\n", HASH_BLOCK, HASH_FACT),
        (".editorconfig", "root = true\n", HASH_BLOCK, HASH_FACT),
        (".editorconfig", "root = true\n", "; one\n; two\n; three\n; four\n", "; A fact.\n"),
        (".envrc.example", "export A=1\n", HASH_BLOCK, HASH_FACT),
        ("alembic.ini", "[alembic]\n", HASH_BLOCK, HASH_FACT),
        ("alembic.ini", "[alembic]\n", "; one\n; two\n; three\n; four\n", "; A fact.\n"),
        ("script.py.mako", "revision = 1\n", HASH_BLOCK, HASH_FACT),
        ("Start App.command", "set -e\n", HASH_BLOCK, HASH_FACT),
        ("requirements.txt", "pydantic\n", HASH_BLOCK, HASH_FACT),
        ("pre-commit", "#!/usr/bin/env bash\nset -e\n", HASH_BLOCK, HASH_FACT),
        ("commit-msg", "#!/usr/bin/env python3\nvalue = 1\n", HASH_BLOCK, HASH_FACT),
        ("page.md", "Some prose.\n", HTML_BLOCK, HTML_FACT),
        ("index.html", "<html>\n", HTML_BLOCK, HTML_FACT),
        ("mark.svg", "<svg>\n", HTML_BLOCK, HTML_FACT),
    ],
)
def test_comment_policy_covers_each_comment_bearing_file_kind(
    tmp_path: Path, name: str, head: str, block: str, fact: str
) -> None:
    long_block = tmp_path / "long" / name
    long_block.parent.mkdir()
    long_block.write_text(f"{head}{block}end\n", encoding="utf-8")
    compliant = tmp_path / "fact" / name
    compliant.parent.mkdir()
    compliant.write_text(f"{head}{fact}end\n", encoding="utf-8")
    start = head.count("\n") + 1

    assert violations([long_block]) == [
        f"{long_block.as_posix()}:{start}: comment block has 4 lines"
    ]
    assert violations([compliant]) == []


def test_comment_policy_skips_an_extensionless_file_without_a_script_shebang(
    tmp_path: Path,
) -> None:
    data = tmp_path / "LICENSE"
    data.write_text(f"Terms.\n{HASH_BLOCK}", encoding="utf-8")

    assert violations([data]) == []


def test_comment_policy_skips_test_fixture_and_cassette_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "tests" / "fixtures" / "posting.html"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(f"<p>Role</p>\n{HTML_BLOCK}", encoding="utf-8")

    assert check_comment_policy(["--files", str(fixture)]) == 0
    capsys.readouterr()


def test_comment_policy_holds_for_this_repository() -> None:
    assert check_comment_policy() == 0
