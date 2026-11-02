"""Importing a CV: it may tidy, it may not invent, and it may not lose anything."""

from __future__ import annotations

import re
import tomllib
import zipfile
from collections.abc import Callable
from math import ceil
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest

from tailorcv.application.config import (
    Effort,
    ProviderName,
    SectionNames,
    Settings,
    Stage,
)
from tailorcv.application.master_cv import parse_canonical_source
from tailorcv.application.master_cv.sources import SourceDocument, read
from tailorcv.application.tailoring.pipeline.importer import (
    Assembled,
    assemble,
    content_tokens,
    tokens,
    verify,
    write_report,
)
from tailorcv.application.tailoring.stages.structure import (
    Dropped,
    ImportedCv,
    ImportedRole,
    ImportedSection,
    Sourced,
)
from tailorcv.shared.errors import IngestError
from tailorcv.shared.results import Finding, Outcome
from tests.support.imported_cv import MESSY_CV, source_from, tidy_extraction
from tests.support.suite import FIXTURES

SECTIONS = SectionNames()


def findings_for(
    source: SourceDocument, extracted: ImportedCv
) -> tuple[dict[str, Finding], Assembled]:
    """Return the findings by name, and the assembled document."""
    built = assemble(extracted, source, SECTIONS)
    return {finding.check: finding for finding in verify(source, extracted, built)}, built


def test_a_bullet_broken_across_lines_is_rejoined() -> None:
    """This is the case that makes line-verbatim conversion useless on a PDF."""
    source = source_from(MESSY_CV)
    _, built = findings_for(source, tidy_extraction())
    assert "cut median end to end latency from 40 minutes" in built.markdown
    assert built.markdown.count("\n- ") == 2


def test_a_packed_header_is_split_into_its_fields() -> None:
    """One source line becomes an organization, a title, and a date range."""
    source = source_from(MESSY_CV)
    _, built = findings_for(source, tidy_extraction())
    assert "### Northwind Analytics" in built.markdown
    assert "**Staff Systems Engineer**" in built.markdown
    assert "2021-Present" in built.markdown


def test_a_tidy_extraction_passes_every_guarantee() -> None:
    source = source_from(MESSY_CV)
    findings, _ = findings_for(source, tidy_extraction())
    assert findings["authentic"].outcome is Outcome.PASS
    assert findings["compliant"].outcome is Outcome.PASS
    assert findings["lossless"].outcome is Outcome.WARN  # the four deliberate drops


@pytest.mark.parametrize(
    ("original", "changed"),
    [
        ("Led architecture and delivery but not people.", "Led architecture and people."),
        ("Supported delivery under the engineering lead.", "Led engineering delivery."),
        (
            "Reduced latency 20 percent and errors 5 percent.",
            "Reduced latency 5 percent and errors 20 percent.",
        ),
        ("Built the platform with the team.", "Built the platform."),
        (
            "Reviewed designs; the team delivered the service.",
            "Delivered the service; the team reviewed designs.",
        ),
    ],
)
def test_import_refuses_changed_professional_meaning(original: str, changed: str) -> None:
    source = source_from(
        MESSY_CV.replace(
            "A senior data platform engineer with eleven years of experience.", original
        )
    )
    extracted = tidy_extraction().model_copy(
        update={"summary": (Sourced(text=changed, lines=(4,)),)}
    )
    findings, _ = findings_for(source, extracted)
    assert any(finding.outcome is Outcome.FAIL for finding in findings.values())


@pytest.mark.parametrize(
    ("written", "rewritten"),
    [
        ("01/2019 - 03/2021", "January 2019 to March 2021"),
        ("2019-21", "2019 to 2021"),
        ("Jan 2019 - Mar 2021", "January 2019 to March 2021"),
    ],
)
def test_a_rewritten_date_fails_authenticity(written: str, rewritten: str) -> None:
    """Dates are copied as the CV writes them, so a rewrite is an invented word."""
    source = source_from(MESSY_CV.replace("2021-Present", written))
    extracted = tidy_extraction()
    role = extracted.roles[0].model_copy(update={"dates": Sourced(text=rewritten, lines=(6,))})
    findings, _ = findings_for(source, extracted.model_copy(update={"roles": (role,)}))
    assert findings["authentic"].outcome is Outcome.FAIL


@pytest.mark.parametrize("change", ["duplicate", "move"])
def test_import_refuses_repeated_or_moved_source_achievements(change: str) -> None:
    source = source_from(
        MESSY_CV
        + "Example Labs | Research Engineer | 2017-2020\nDeveloped laboratory automation.\n"
    )
    extracted = tidy_extraction()
    first = extracted.roles[0]
    second = ImportedRole(
        organisation=Sourced(text="Example Labs", lines=(13,)),
        title=Sourced(text="Research Engineer", lines=(13,)),
        dates=Sourced(text="2017 to 2020", lines=(13,)),
        bullets=(Sourced(text="Developed laboratory automation.", lines=(14,)),),
    )
    if change == "duplicate":
        first = first.model_copy(update={"bullets": (*first.bullets, first.bullets[0])})
    else:
        first, second = (
            first.model_copy(update={"bullets": second.bullets}),
            second.model_copy(update={"bullets": first.bullets}),
        )
    findings, _ = findings_for(source, extracted.model_copy(update={"roles": (first, second)}))
    assert any(item.outcome is Outcome.FAIL for item in findings.values())


def test_a_word_from_nowhere_fails_authenticity() -> None:
    """The whole point: a model that improves your bullet gets caught."""
    source = source_from(MESSY_CV)
    extracted = tidy_extraction()
    role = extracted.roles[0]
    tampered = role.model_copy(
        update={
            "bullets": (
                Sourced(text="Rebuilt the ingestion tier, saving 4 million dollars.", lines=(7, 8)),
                *role.bullets[1:],
            )
        }
    )
    findings, _ = findings_for(source, extracted.model_copy(update={"roles": (tampered,)}))
    assert findings["authentic"].outcome is Outcome.FAIL
    assert "million" in findings["authentic"].detail or "dollars" in findings["authentic"].detail


def test_citing_the_wrong_line_fails_even_when_the_words_are_real() -> None:
    """Provenance has to mean something, so the citation is checked, not just the text."""
    source = source_from(MESSY_CV)
    extracted = tidy_extraction()
    role = extracted.roles[0]
    miscited = role.model_copy(
        update={"bullets": (Sourced(text=role.bullets[0].text, lines=(9,)), *role.bullets[1:])}
    )
    findings, _ = findings_for(source, extracted.model_copy(update={"roles": (miscited,)}))
    assert findings["authentic"].outcome is Outcome.FAIL


def test_a_field_citing_nothing_fails() -> None:
    source = source_from(MESSY_CV)
    extracted = tidy_extraction()
    findings, _ = findings_for(
        source, extracted.model_copy(update={"name": Sourced(text="Jane Q. Example", lines=())})
    )
    assert findings["authentic"].outcome is Outcome.FAIL
    assert "cite no source line" in findings["authentic"].detail


def test_a_silently_lost_employer_fails_losslessness() -> None:
    """Losing a whole role quietly is the worst thing this can do."""
    source = source_from(MESSY_CV)
    extracted = tidy_extraction().model_copy(update={"roles": ()})
    findings, _ = findings_for(source, extracted)
    assert findings["lossless"].outcome is Outcome.FAIL
    assert "northwind" in findings["lossless"].detail.lower()


def test_a_lost_metric_fails_losslessness() -> None:
    """A number is exactly the kind of word nobody can afford to drop."""
    source = source_from(MESSY_CV)
    extracted = tidy_extraction()
    role = extracted.roles[0]
    shortened = role.model_copy(
        update={
            "bullets": (
                Sourced(text="Rebuilt the ingestion tier on Kafka.", lines=(7, 8)),
                *role.bullets[1:],
            )
        }
    )
    findings, _ = findings_for(source, extracted.model_copy(update={"roles": (shortened,)}))
    assert findings["lossless"].outcome is Outcome.FAIL
    assert "212" in findings["lossless"].detail or "40" in findings["lossless"].detail


def test_a_field_missing_from_the_assembled_document_fails_losslessness() -> None:
    """Losslessness is a property of the document, not of the extraction alone."""
    source = source_from(MESSY_CV)
    extracted = tidy_extraction()
    built = assemble(extracted, source, SECTIONS)
    degree = extracted.education[0].text
    assert degree in built.markdown

    without = Assembled(
        markdown=built.markdown.replace(degree, ""),
        dropped=built.dropped,
        bullets=built.bullets,
    )
    findings = {finding.check: finding for finding in verify(source, extracted, without)}

    assert findings["lossless"].outcome is Outcome.FAIL
    assert "bachelor" in findings["lossless"].detail.lower()


def test_dropping_a_line_on_purpose_is_reported_not_hidden() -> None:
    source = source_from(MESSY_CV)
    findings, built = findings_for(source, tidy_extraction())
    assert "Page 1 of 2" in [line.text for line, _ in built.dropped]
    assert findings["lossless"].outcome is Outcome.WARN


def test_losing_a_small_word_does_not_fail_the_run() -> None:
    """A check that cries wolf over "the" is a check nobody reads."""
    assert "the" not in content_tokens("Rebuilt the ingestion tier")
    assert "kafka" in content_tokens("Rebuilt on Kafka")
    assert "212" in content_tokens("across 212 feeds")


def test_tokens_are_compared_case_insensitively() -> None:
    assert tokens("Kafka") == tokens("kafka")


def test_run_together_bullets_fail_compliance() -> None:
    from tailorcv.application.tailoring.pipeline.importer import _compliant

    run_on = (
        "# Jane\n\njane@example.com\n\n## Professional Experience\n\n"
        "### Acme\n\n**Engineer**  \n2021 to Present | Springfield\n"
        "- One achievement.\n- Another achievement.\n"
    )
    assert _compliant(run_on, expected_bullets=2).outcome is Outcome.FAIL


def test_compliance_is_checked_without_writing_the_cv_to_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check parses the text it holds, with no temporary file.

    A temporary file would put the whole career record on disk, world-readable,
    for the length of the check.
    """
    import tempfile

    source = source_from(MESSY_CV)
    extracted = tidy_extraction()

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the import checks wrote the CV to a file")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", refuse)
    monkeypatch.setattr(Path, "write_text", refuse)

    findings, _ = findings_for(source, extracted)
    assert findings["compliant"].outcome is Outcome.PASS


def _docx_paragraph(text: str, bold: bool) -> str:
    """Write one paragraph, turning a tab into a tab run and a newline into a break."""
    properties = "<w:rPr><w:b/></w:rPr>" if bold else ""
    runs: list[str] = []
    for index, segment in enumerate(text.split("\n")):
        if index:
            runs.append("<w:r><w:br/></w:r>")
        for position, piece in enumerate(segment.split("\t")):
            if position:
                runs.append("<w:r><w:tab/></w:r>")
            runs.append(f"<w:r>{properties}<w:t>{escape(piece)}</w:t></w:r>")
    # A tab stop definition is also a w:tab element, and it is not text.
    stops = '<w:pPr><w:tabs><w:tab w:val="right" w:pos="9000"/></w:tabs></w:pPr>'
    has_tab = "\t" in text
    return f"<w:p>{stops if has_tab else ''}{''.join(runs)}</w:p>"


def docx_at(
    path: Path,
    paragraphs: list[tuple[str, bool]],
    header: list[tuple[str, bool]] | None = None,
    footer: list[tuple[str, bool]] | None = None,
) -> Path:
    """Write a minimal but real .docx, so the reader is tested on the format."""
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def part(root: str, inner: list[tuple[str, bool]], body: bool = False) -> str:
        content = "".join(_docx_paragraph(text, bold) for text, bold in inner)
        if body:
            content = f"<w:body>{content}</w:body>"
        return f'<?xml version="1.0"?><w:{root} xmlns:w="{namespace}">{content}</w:{root}>'

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", part("document", paragraphs, body=True))
        if header:
            archive.writestr("word/header1.xml", part("hdr", header))
        if footer:
            archive.writestr("word/footer1.xml", part("ftr", footer))
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
    return path


def test_a_word_document_is_read_without_a_word_library(tmp_path: Path) -> None:
    paragraphs = [(line, False) for line in MESSY_CV.splitlines() if line.strip()]
    paragraphs[0] = ("Jane Q. Example", True)
    source = read(docx_at(tmp_path / "cv.docx", paragraphs))
    assert source.method == "Word document"
    assert source.lines[0].text == "Jane Q. Example"
    assert source.lines[0].emphasised


def test_a_corrupt_word_file_says_what_to_do(tmp_path: Path) -> None:
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a zip")
    with pytest.raises(IngestError, match="save as .docx"):
        read(path)


def test_an_unsupported_format_names_what_is_supported(tmp_path: Path) -> None:
    path = tmp_path / "cv.rtf"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(IngestError, match=r"\.docx"):
        read(path)


def test_a_nearly_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cv.txt"
    path.write_text("Jane\nExample\n", encoding="utf-8")
    with pytest.raises(IngestError, match="screenshot it"):
        read(path)


def test_the_schema_cannot_cite_a_line_that_does_not_exist() -> None:
    from pydantic import ValidationError

    from tailorcv.application.tailoring.stages.structure import response_schema

    schema = response_schema(source_from(MESSY_CV))
    with pytest.raises(ValidationError):
        schema.model_validate({"name": {"text": "Jane", "lines": [999]}})


def test_the_schema_rejects_an_answer_with_an_undeclared_key() -> None:
    """An answer carrying a field this schema never declared is refused, not dropped."""
    from pydantic import ValidationError

    from tailorcv.application.tailoring.stages.structure import response_schema

    schema = response_schema(source_from(MESSY_CV))
    with pytest.raises(ValidationError):
        schema.model_validate({"name": {"text": "Jane", "lines": [1]}, "extra_field": "x"})


def test_the_report_shows_where_every_field_came_from(tmp_path: Path) -> None:
    from tailorcv.application.tailoring.pipeline.importer import ImportResult

    source = source_from(MESSY_CV)
    extracted = tidy_extraction()
    built = assemble(extracted, source, SECTIONS)
    result = ImportResult(
        markdown=built.markdown,
        source=source,
        extracted=extracted,
        dropped=tuple(built.dropped),
        findings=verify(source, extracted, built),
    )
    report = write_report(tmp_path, result).read_text(encoding="utf-8")
    assert "Where each line came from" in report
    assert "Page 1 of 2" in report
    assert "page footer" in report


def test_an_imported_cv_can_then_be_tailored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of importing is that the rest of the tool then works."""
    from tailorcv.application.config import Settings
    from tailorcv.application.tailoring.pipeline import RunCommand, import_cv, run
    from tests.support.doubles import ScriptedProvider
    from tests.support.payloads import script
    from tests.support.suite import POSTING, SUITE_KEY_VARIABLE

    original = tmp_path / "old.txt"
    original.write_text(MESSY_CV, encoding="utf-8")
    extracted = tidy_extraction()
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    monkeypatch.setattr(
        "tailorcv.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: ScriptedProvider(
            {"ImportedCv": lambda schema: extracted.model_dump(mode="json")}
        ),
    )
    destination = tmp_path / "CV_Master.md"
    imported = import_cv(
        original, Settings(cv=destination), destination=destination, cache_enabled=False
    )
    assert imported.passed

    monkeypatch.setattr(
        "tailorcv.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: ScriptedProvider(script({})),
    )
    posting = tmp_path / "posting.txt"
    posting.write_text(POSTING, encoding="utf-8")
    result = run(
        RunCommand(
            source=str(posting),
            settings=Settings(cv=destination, output_dir=tmp_path / "runs", pages=1),
            cache_enabled=False,
        )
    )
    assert result.materialized is not None
    assert result.materialized.document.is_file()


def test_an_import_writes_both_files_owner_only_and_the_report_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both carry the whole career record, so neither is readable by other users.

    The order matters too. The two writes are not one operation, so interrupted
    between them this order leaves an audit report with no CV, which reads as
    the failure it was, rather than a converted CV with no record of what was
    dropped, which reads as a finished import.
    """
    from tailorcv.application.config import Settings
    from tailorcv.application.tailoring.pipeline import import_cv
    from tests.support.doubles import ScriptedProvider
    from tests.support.suite import SUITE_KEY_VARIABLE

    original = tmp_path / "old.txt"
    original.write_text(MESSY_CV, encoding="utf-8")
    extracted = tidy_extraction()
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    monkeypatch.setattr(
        "tailorcv.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: ScriptedProvider(
            {"ImportedCv": lambda schema: extracted.model_dump(mode="json")}
        ),
    )

    from tailorcv.infrastructure.artifacts.files import write_private

    order: list[str] = []

    def noting(path: Path, text: str) -> Path:
        order.append(path.name)
        return write_private(path, text)

    monkeypatch.setattr("tailorcv.application.tailoring.pipeline.importer.write_private", noting)
    monkeypatch.setattr("tailorcv.application.tailoring.pipeline.service.write_private", noting)

    destination = tmp_path / "CV_Master.md"
    result = import_cv(
        original, Settings(cv=destination), destination=destination, cache_enabled=False
    )

    assert order == ["Import_Report.md", "CV_Master.md"]
    assert result.report is not None
    for path in (result.report, destination):
        assert path.stat().st_mode & 0o077 == 0, f"{path.name} is readable by others"


def test_straightening_a_curly_apostrophe_does_not_look_like_a_lost_name() -> None:
    """The importer folds typographic punctuation, so a curly O'Brien is no lost name."""
    from tailorcv.application.tailoring.pipeline.importer import content_tokens

    assert content_tokens("O’Brien") == content_tokens("O'Brien")


def test_folding_punctuation_does_not_hide_a_genuinely_lost_word() -> None:
    """The fold must be narrow: punctuation only, never letters."""
    from tailorcv.application.tailoring.pipeline.importer import content_tokens

    assert content_tokens("Neo4j and Cypher") != content_tokens("Neo4j")


def test_every_dash_a_word_processor_writes_folds_to_a_hyphen() -> None:
    from tailorcv.application.tailoring.pipeline.importer import plain

    assert plain("2021–2024") == "2021-2024"
    assert plain("scope — wide") == "scope - wide"
    assert plain("a b") == "a b"
    assert plain("“quoted”") == '"quoted"'
    assert plain("wait…") == "wait..."


def test_an_accent_is_folded_by_the_importer_rather_than_by_the_model() -> None:
    """A transliteration a model performed is a word in no cited source line."""
    from tailorcv.application.tailoring.pipeline.importer import plain

    assert plain("Zoë Müller") == "Zoe Muller"
    assert tokens("Zoë Müller") == tokens("Zoe Muller")


def test_a_character_with_no_ascii_form_is_kept_rather_than_guessed_at() -> None:
    """Inventing a spelling for somebody's name is worse than reporting it."""
    from tailorcv.application.tailoring.pipeline.importer import plain

    assert plain("Straße") == "Straße"


def named_cv(name: str) -> tuple[SourceDocument, ImportedCv]:
    """Return a minimal CV whose only interesting feature is how a name is spelled."""
    source = source_from(f"{name}\nEXPERIENCE\nHelix Analytics\n")
    extracted = ImportedCv(
        name=Sourced(text=name, lines=(1,)),
        roles=(ImportedRole(organisation=Sourced(text="Helix Analytics", lines=(3,))),),
        dropped=(Dropped(line=2, reason="section heading, replaced by the structure"),),
    )
    return source, extracted


def test_an_accented_name_reaches_the_master_cv_as_ascii() -> None:
    """The point of folding in code: the document, not only the comparison."""
    findings, built = findings_for(*named_cv("Zoë Müller"))
    assert "Zoe Muller" in built.markdown
    assert built.markdown.isascii()
    assert findings["authentic"].outcome is Outcome.PASS
    assert findings["characters"].outcome is Outcome.PASS
    assert "LATIN SMALL LETTER E WITH DIAERESIS" in findings["characters"].detail


def test_a_character_the_fold_cannot_reach_is_reported_against_the_cv() -> None:
    """A warning, because the converted document is still what they have to edit."""
    findings, built = findings_for(*named_cv("Anders Strøm"))
    assert "Strøm" in built.markdown
    assert findings["characters"].outcome is Outcome.WARN
    assert "LATIN SMALL LETTER O WITH STROKE" in findings["characters"].detail


def test_a_cv_that_needed_no_folding_says_so() -> None:
    findings, _ = findings_for(*named_cv("Jane Q. Example"))
    assert findings["characters"].detail == "nothing needed folding"


def test_a_screenshot_of_a_cv_is_transcribed_by_the_reader_it_is_given(tmp_path: Path) -> None:
    """The documented route for a CV that exists only as an image."""
    path = tmp_path / "cv.png"
    path.write_bytes(b"not really a PNG, and nothing here decodes it")
    seen: list[str] = []

    def transcribe(images: list[bytes], media_type: str) -> str:
        seen.append(media_type)
        return MESSY_CV

    source = read(path, vision=transcribe)
    assert seen == ["image/png"]
    assert source.method == "screenshot"
    assert source.lines[0].text == "Jane Q. Example"


def test_reading_a_screenshot_with_no_model_available_says_what_to_check(
    tmp_path: Path,
) -> None:
    """It needs a paid call, so the failure names the command that diagnoses it."""
    path = tmp_path / "cv.png"
    path.write_bytes(b"x")
    with pytest.raises(IngestError, match="tailorcv doctor"):
        read(path)


CV_FORMATS = FIXTURES / "cv_formats"
SHAPES = (
    "europass_docx",
    "academic_pdf",
    "functional_md",
    "linkedin_txt",
    "two_column_pdf",
    "long_md",
)
MARKUP = re.compile(r"^(?:#{1,6}\s+|[-*+\u2022]\s+|\d{1,3}[.)]\s+)")


def cv_format(shape: str) -> tuple[Path, dict[str, Any]]:
    """Return one corpus CV and the fields a faithful extraction holds."""
    expected = tomllib.loads((CV_FORMATS / shape / "expected.toml").read_text(encoding="utf-8"))
    return CV_FORMATS / shape / expected["source"], expected


def _flat(text: str) -> str:
    """Drop list and heading markup and bold markers, and collapse whitespace."""
    return " ".join(MARKUP.sub("", text.strip()).replace("**", "").split())


def _within(wanted: str, text: str) -> bool:
    """Return whether ``wanted`` occurs in ``text`` as whole words."""
    return re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", text) is not None


def _cite(source: SourceDocument, text: str, start: int = 1) -> tuple[int, ...]:
    """Return the lines holding ``text`` from ``start`` on, preferring an exact span."""
    lines = [line for line in source.lines if line.number >= start]
    wanted = _flat(text)
    for exact in (True, False):
        for width in range(1, 5):
            for index in range(len(lines) - width + 1):
                span = lines[index : index + width]
                joined = " ".join(_flat(line.text) for line in span)
                if joined == wanted if exact else _within(wanted, joined):
                    return tuple(line.number for line in span)
    raise AssertionError(f"{text!r} is in no line of {source.origin} from line {start}")


def _correct_extraction(source: SourceDocument, expected: dict[str, Any]) -> ImportedCv:
    """Build the extraction a faithful model returns for one corpus CV."""

    def top(key: str) -> tuple[Sourced, ...]:
        return tuple(
            Sourced(text=text, lines=_cite(source, text)) for text in expected.get(key, [])
        )

    roles = []
    for role in expected["roles"]:
        organisation = Sourced(text=role["employer"], lines=_cite(source, role["employer"]))
        cursor = anchor = organisation.lines[0]
        fields: dict[str, Any] = {}
        for key in ("title", "dates", "location"):
            if key in role:
                # A packed line can hold the dates above the title line.
                try:
                    lines = _cite(source, role[key], cursor)
                except AssertionError:
                    lines = _cite(source, role[key], anchor)
                fields[key] = Sourced(text=role[key], lines=lines)
                cursor = max(cursor, lines[0])
        bullets = []
        for text in role["bullets"]:
            bullets.append(Sourced(text=text, lines=_cite(source, text, cursor)))
            cursor = bullets[-1].lines[-1]
        roles.append(ImportedRole(organisation=organisation, bullets=tuple(bullets), **fields))
    return ImportedCv(
        name=Sourced(text=expected["name"], lines=_cite(source, expected["name"])),
        contact=top("contact"),
        summary=top("summary"),
        skills=top("skills"),
        roles=tuple(roles),
        education=top("education"),
        publications=top("publications"),
        sections=tuple(
            ImportedSection(
                heading=Sourced(text=section["heading"], lines=_cite(source, section["heading"])),
                items=tuple(
                    Sourced(text=text, lines=_cite(source, text)) for text in section["items"]
                ),
            )
            for section in expected.get("sections", [])
        ),
        dropped=tuple(
            Dropped(line=_cite(source, text)[0], reason="heading or page furniture")
            for text in expected["dropped"]
        ),
    )


@pytest.mark.parametrize("shape", SHAPES)
def test_the_reader_keeps_every_field_of_each_cv_format_whole(shape: str) -> None:
    path, expected = cv_format(shape)
    document = " ".join(_flat(line.text) for line in read(path).lines)
    fields = [expected["name"]]
    for key in ("contact", "summary", "skills", "education", "publications", "dropped"):
        fields += expected.get(key, [])
    for section in expected.get("sections", []):
        fields += [section["heading"], *section["items"]]
    for role in expected["roles"]:
        fields += [role[key] for key in ("employer", "title", "dates", "location") if key in role]
        fields += role["bullets"]
    missing = [field for field in fields if not _within(_flat(field), document)]
    assert missing == []


@pytest.mark.parametrize("shape", SHAPES)
def test_a_correct_extraction_of_each_cv_format_passes_verify(shape: str) -> None:
    path, expected = cv_format(shape)
    source = read(path)
    findings, _ = findings_for(source, _correct_extraction(source, expected))
    failed = {name for name, finding in findings.items() if finding.outcome is Outcome.FAIL}
    assert failed == set(expected.get("failing_checks", []))


def test_grants_keep_their_own_heading_instead_of_joining_the_publications() -> None:
    """A grant filed under Publications is a citation the CV never made."""
    path, expected = cv_format("academic_pdf")
    source = read(path)
    _, built = findings_for(source, _correct_extraction(source, expected))
    grant = "Instrument Data Commons, Example Science Foundation, Co-investigator, 2021."
    heading, _, below = built.markdown.partition("## GRANTS AND FUNDING")
    assert grant in below
    assert grant not in heading


def test_achievements_grouped_by_skill_survive_under_their_own_heading() -> None:
    """A functional CV hangs its achievements off a skill, not off an employer."""
    path, expected = cv_format("functional_md")
    source = read(path)
    findings, built = findings_for(source, _correct_extraction(source, expected))
    assert "## Achievements by Skill" in built.markdown
    assert "Standardised the month-end checklist across 4 regional teams." in built.markdown
    assert findings["lossless"].outcome is not Outcome.FAIL


# JSON carrying quoted keys, digits, and line-number arrays packs fewer
# characters into a token than prose does.
CHARACTERS_PER_TOKEN = 3

# High-effort reasoning is billed inside the same ceiling, so the structure
# budget has to clear the echoed object by this much.
STRUCTURE_REASONING_HEADROOM = 7500


def _echoed_tokens(shape: str) -> int:
    """Return a conservative token count for the object a faithful model writes back."""
    path, expected = cv_format(shape)
    source = read(path)
    echoed = _correct_extraction(source, expected).model_dump_json()
    return ceil(len(echoed) / CHARACTERS_PER_TOKEN)


def test_the_structure_ceiling_fits_the_longest_cv_and_still_funds_reasoning() -> None:
    """Reasoning is billed inside ``max_tokens``, so the echoed CV bounds the thinking."""
    settings = Settings(provider=ProviderName.ANTHROPIC)
    ceiling = settings.policy_for(Stage.STRUCTURE).max_tokens
    echoed = _echoed_tokens("long_md")

    assert settings.choice_for_stage(Stage.STRUCTURE).effort is Effort.HIGH
    assert echoed > ceiling // 2
    assert ceiling - echoed >= STRUCTURE_REASONING_HEADROOM


def test_a_pdf_heading_reaches_the_reader_emphasised() -> None:
    """A PDF carries emphasis only in its fonts, and emphasis is what marks a heading."""
    path, _ = cv_format("academic_pdf")
    emphasised = {line.text for line in read(path).lines if line.emphasised}

    assert "SUMMARY" in emphasised
    assert "Acme Instruments" in emphasised
    assert not [text for text in emphasised if text.startswith(("PhD,", "Applications Scientist"))]


def test_a_two_column_pdf_is_read_column_by_column() -> None:
    """Following the stream reads across the gutter, running the sidebar into the roles."""
    path, _ = cv_format("two_column_pdf")
    texts = [line.text for line in read(path).lines]
    assert "Terraform, Kubernetes" in texts
    assert texts.index("Example University, 2013 to 2015") < texts.index("Acme Logistics")


def _shortened_title(extracted: ImportedCv) -> ImportedCv:
    first = extracted.roles[0]
    assert first.title is not None
    shorter = first.model_copy(
        update={"title": Sourced(text="Planning Analyst", lines=first.title.lines)}
    )
    return extracted.model_copy(update={"roles": (shorter, *extracted.roles[1:])})


def _dropped_role(extracted: ImportedCv) -> ImportedCv:
    *kept, last = extracted.roles
    pieces = [last.organisation, last.title, last.dates, last.location, *last.bullets]
    lines = sorted({number for piece in pieces if piece for number in piece.lines})
    dropped = tuple(Dropped(line=number, reason="older role, left out") for number in lines)
    return extracted.model_copy(
        update={"roles": tuple(kept), "dropped": (*extracted.dropped, *dropped)}
    )


def _merged_employers(extracted: ImportedCv) -> ImportedCv:
    *kept, last = extracted.roles
    merged = last.model_copy(update={"organisation": extracted.roles[0].organisation})
    return extracted.model_copy(update={"roles": (*kept, merged)})


@pytest.mark.parametrize("damage", [_shortened_title, _dropped_role, _merged_employers])
def test_a_wrong_extraction_of_a_profile_export_fails_verify(
    damage: Callable[[ImportedCv], ImportedCv],
) -> None:
    path, expected = cv_format("linkedin_txt")
    source = read(path)
    findings, _ = findings_for(source, damage(_correct_extraction(source, expected)))
    assert findings["lossless"].outcome is Outcome.FAIL


def test_a_reordered_citation_fails_authenticity() -> None:
    """Every word is cited and real, and the citation still names the wrong first author."""
    path, expected = cv_format("academic_pdf")
    source = read(path)
    extracted = _correct_extraction(source, expected)
    swapped = extracted.publications[0].model_copy(
        update={
            "text": expected["publications"][0].replace("Example D, Roe B.", "Roe B, Example D.")
        }
    )
    damaged = extracted.model_copy(update={"publications": (swapped, *extracted.publications[1:])})
    findings, _ = findings_for(source, damaged)
    assert findings["authentic"].outcome is Outcome.FAIL


def test_a_field_citing_its_lines_out_of_order_still_copies_them() -> None:
    """The citation list is the model's order; the words are checked against the document's."""
    source = source_from(
        "Jane Q. Example\nEXPERIENCE\nHelix Analytics\nBachelor of Science\nin Data Engineering\n"
    )
    extracted = ImportedCv(
        name=Sourced(text="Jane Q. Example", lines=(1,)),
        roles=(ImportedRole(organisation=Sourced(text="Helix Analytics", lines=(3,))),),
        education=(Sourced(text="Bachelor of Science in Data Engineering", lines=(5, 4)),),
        dropped=(Dropped(line=2, reason="section heading, replaced by the structure"),),
    )

    findings, _ = findings_for(source, extracted)

    assert findings["authentic"].outcome is Outcome.PASS


def test_a_word_taken_out_of_a_copied_field_fails_authenticity() -> None:
    """A lowercase word is no content token, so losing one out of a degree only shows in order."""
    path, expected = cv_format("linkedin_txt")
    source = read(path)
    extracted = _correct_extraction(source, expected)
    shortened = extracted.education[0].model_copy(
        update={"text": expected["education"][0].replace(" of Science", " Science")}
    )
    findings, _ = findings_for(source, extracted.model_copy(update={"education": (shortened,)}))
    assert findings["lossless"].outcome is not Outcome.FAIL
    assert findings["authentic"].outcome is Outcome.FAIL


@pytest.mark.parametrize(
    "furniture",
    [
        "(continued)",
        "References available on request",
        "- 2 -",
        "CONTACT",
        "Curriculum Vitae",
        "RESUME",
        "Jane Q. Example - Curriculum Vitae",
        "Jane Q. Example | Page 1",
        "Confidential",
        "Continued on next page",
        "Generated by Resume Builder",
    ],
)
def test_dropping_a_line_the_prompt_calls_furniture_stays_a_warning(furniture: str) -> None:
    source = source_from(MESSY_CV.replace("Page 1 of 2", furniture))
    findings, _ = findings_for(source, tidy_extraction())
    assert findings["lossless"].outcome is Outcome.WARN


@pytest.mark.parametrize("heading", ["Key Achievements", "Selected Projects", "Interests"])
def test_a_heading_over_placed_content_drops_with_a_warning(heading: str) -> None:
    source = source_from(MESSY_CV.replace("SUMMARY", heading))
    findings, _ = findings_for(source, tidy_extraction())
    assert findings["lossless"].outcome is Outcome.WARN


def _dropping(extracted: ImportedCv, line: int, bullet: int) -> ImportedCv:
    """Move one bullet of the first role into the dropped list."""
    role = extracted.roles[0]
    bullets = tuple(piece for index, piece in enumerate(role.bullets) if index != bullet)
    return extracted.model_copy(
        update={
            "roles": (role.model_copy(update={"bullets": bullets}),),
            "dropped": (*extracted.dropped, Dropped(line=line, reason="heading")),
        }
    )


@pytest.mark.parametrize(
    "achievement",
    [
        "Designed the schema registry and its compatibility rules.",
        "Led Kafka migration",
        "- Led Kafka migration",
    ],
)
def test_a_dropped_achievement_fails_losslessness(achievement: str) -> None:
    source = source_from(
        MESSY_CV.replace("Designed the schema registry and its compatibility rules.", achievement)
    )
    findings, _ = findings_for(source, _dropping(tidy_extraction(), line=9, bullet=1))
    assert findings["lossless"].outcome is Outcome.FAIL


def test_a_dropped_copy_of_a_repeated_achievement_fails_losslessness() -> None:
    """A repeated line is a running header only when short or at a page boundary."""
    repeated = "Designed the schema registry and its compatibility rules."
    source = source_from(
        MESSY_CV
        + f"Example Labs | Research Engineer | 2017-2020\n{repeated}\n"
        + "Developed laboratory automation.\n"
    )
    extracted = tidy_extraction()
    second = ImportedRole(
        organisation=Sourced(text="Example Labs", lines=(13,)),
        title=Sourced(text="Research Engineer", lines=(13,)),
        dates=Sourced(text="2017-2020", lines=(13,)),
        bullets=(Sourced(text="Developed laboratory automation.", lines=(15,)),),
    )
    damaged = extracted.model_copy(
        update={
            "roles": (*extracted.roles, second),
            "dropped": (*extracted.dropped, Dropped(line=14, reason="repeated header")),
        }
    )
    findings, _ = findings_for(source, damaged)
    assert findings["lossless"].outcome is Outcome.FAIL


@pytest.mark.parametrize(
    ("broken", "joined"),
    [
        ("Led cross-\nfunctional teams", "Led cross-functional teams"),
        ("Co-\nfounder, Example Labs", "Co-founder, Example Labs"),
        ("Opti\x02mised nightly runs", "Optimised nightly runs"),
        ("Opti\x02\nmised nightly runs", "Optimised nightly runs"),
        ("Opti\u00ad\nmised nightly runs", "Optimised nightly runs"),
    ],
)
def test_the_reader_rejoins_a_word_a_line_break_split(
    tmp_path: Path, broken: str, joined: str
) -> None:
    path = tmp_path / "cv.txt"
    path.write_text(MESSY_CV.replace("SUMMARY", broken), encoding="utf-8")
    assert read(path).lines[2].text == joined


def test_a_functional_markdown_cv_goes_to_the_model_instead_of_being_adopted() -> None:
    path, _ = cv_format("functional_md")
    assert parse_canonical_source(path, path.read_bytes()) is None


def test_a_canonical_markdown_cv_is_still_adopted_without_the_model() -> None:
    path = FIXTURES / "master_cv.md"
    inventory = parse_canonical_source(path, path.read_bytes())
    assert inventory is not None
    assert inventory.roles
