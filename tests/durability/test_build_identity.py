"""Build provenance identifies bytes without requiring Git in installed apps."""

from pathlib import Path

from clearscout.infrastructure import build


def test_source_changes_change_build_identity_without_changing_package_version(tmp_path: Path):
    package = tmp_path / "clearscout"
    package.mkdir()
    source = package / "example.py"
    source.write_text("VALUE = 1\n")
    before = build.identify_build(package, "0.1.0")
    source.write_text("VALUE = 2\n")
    after = build.identify_build(package, "0.1.0")
    assert before.revision != after.revision
    assert before.package_version == after.package_version == "0.1.0"
    assert before.revision.startswith("sha256:")


def test_distribution_record_identifies_an_install_without_source(tmp_path, monkeypatch):
    monkeypatch.setattr(
        build, "_distribution_record", lambda: "clearscout/module.so,sha256=abc,123"
    )
    identity = build.identify_build(tmp_path, "0.1.0")
    assert identity.revision.startswith("record-sha256:")
    monkeypatch.setattr(build, "_distribution_record", lambda: "")
    assert build.identify_build(tmp_path, "0.1.0").revision == ""


def test_dirty_git_checkout_keeps_both_commit_and_content_identity(tmp_path, monkeypatch):
    (tmp_path / "example.py").write_text("VALUE = 1\n")
    monkeypatch.setattr(build, "_git_identity", lambda _: "git:abc+dirty")
    identity = build.identify_build(tmp_path, "0.1.0")
    assert identity.revision.startswith("git:abc+dirty/sha256:")
