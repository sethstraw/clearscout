from tailorcv.application.master_cv import anchoring
from tailorcv.application.master_cv.anchoring import Anchor, reanchor_many

OLD_CV = """## Professional Experience

### Northwind Analytics

**Staff Systems Engineer - Platform**
March 2021 to Present | Springfield, Illinois

- Rebuilt the ingestion tier on Kafka, cutting median latency from 40 minutes to 90 seconds
- Introduced data quality rules covering 40 sites, with failures routed to the owning team
- Wrote the Terraform modules that provision every environment

### Contoso Health

**Senior Data Engineer**
June 2017 to February 2021 | Chicago, Illinois

- Owned the nightly clinical load and the definitions behind it
"""

QUOTE = "Introduced data quality rules covering 40 sites, with failures routed to the owning team"


def anchor_for(text: str, quote: str) -> Anchor:
    at = text.index(quote)
    return Anchor(quote=quote, char_start=at, char_end=at + len(quote))


def edited(replacement: str, quote: str = QUOTE) -> str:
    return OLD_CV.replace(quote, replacement)


def test_unchanged_document_reports_no_drift():
    [drift] = reanchor_many(OLD_CV, OLD_CV, [anchor_for(OLD_CV, QUOTE)])

    assert drift.found
    assert drift.text == QUOTE
    assert not drift.changed
    assert not drift.material


def test_insertion_before_the_anchor_shifts_it_without_drift():
    new_cv = "## Summary\n\nBuilds data platforms for regulated industries.\n\n" + OLD_CV

    [drift] = reanchor_many(OLD_CV, new_cv, [anchor_for(OLD_CV, QUOTE)])

    assert drift.found
    assert drift.text == QUOTE
    assert not drift.changed


def test_rewording_inside_the_anchor_is_cosmetic():
    new_cv = edited(
        "Introduced data quality rules covering 40 sites, with failures sent to the owning team"
    )

    [drift] = reanchor_many(OLD_CV, new_cv, [anchor_for(OLD_CV, QUOTE)])

    assert drift.found
    assert drift.text == (
        "Introduced data quality rules covering 40 sites, with failures sent to the owning team"
    )
    assert drift.changed
    assert not drift.material


def test_quote_moved_to_another_section_is_still_found():
    without = OLD_CV.replace(f"- {QUOTE}\n", "")
    new_cv = without.replace(
        "- Owned the nightly clinical load",
        f"- {QUOTE}\n- Owned the nightly clinical load",
    )

    [drift] = reanchor_many(OLD_CV, new_cv, [anchor_for(OLD_CV, QUOTE)])

    assert drift.found
    assert drift.text == QUOTE
    assert not drift.changed


def test_deleted_quote_is_reported_as_not_found():
    new_cv = OLD_CV.replace(f"- {QUOTE}\n", "")

    [drift] = reanchor_many(OLD_CV, new_cv, [anchor_for(OLD_CV, QUOTE)])

    assert not drift.found
    assert drift.text == ""
    assert not drift.changed
    assert not drift.material


def test_duplicate_quote_still_resolves():
    # Drift reports the anchored text rather than its offset, so what this pins
    # down is that two identical occurrences resolve to one of them instead of
    # being refused as ambiguous.
    without = OLD_CV.replace(f"- {QUOTE}\n", "")
    new_cv = without.replace(
        "- Owned the nightly clinical load and the definitions behind it\n",
        f"- {QUOTE}\n- Owned the nightly clinical load and the definitions behind it\n- {QUOTE}\n",
    )

    [drift] = reanchor_many(OLD_CV, new_cv, [anchor_for(OLD_CV, QUOTE)])

    assert drift.found
    assert drift.text == QUOTE
    assert not drift.changed


def test_whitespace_change_is_cosmetic():
    new_cv = edited(QUOTE.replace("40 sites, with", "40 sites,  with"))

    [drift] = reanchor_many(OLD_CV, new_cv, [anchor_for(OLD_CV, QUOTE)])

    assert drift.found
    assert drift.changed
    assert not drift.material
    assert "  with" in drift.text


def test_changed_figure_is_material():
    new_cv = edited(QUOTE.replace("40 sites", "400 sites"))

    [drift] = reanchor_many(OLD_CV, new_cv, [anchor_for(OLD_CV, QUOTE)])

    assert drift.found
    assert "400 sites" in drift.text
    assert drift.changed
    assert drift.material
    assert "figures" in drift.reason


def test_dropped_figure_is_material():
    new_cv = edited(QUOTE.replace("covering 40 sites", "covering every site"))

    [drift] = reanchor_many(OLD_CV, new_cv, [anchor_for(OLD_CV, QUOTE)])

    assert drift.changed
    assert drift.material


def test_added_negation_is_material():
    new_cv = edited(QUOTE.replace("failures routed", "failures not routed"))

    [drift] = reanchor_many(OLD_CV, new_cv, [anchor_for(OLD_CV, QUOTE)])

    assert drift.found
    assert drift.changed
    assert drift.material
    assert "negation" in drift.reason


def test_changed_figure_and_added_negation_are_both_reported():
    new_cv = edited(
        QUOTE.replace(
            "covering 40 sites, with failures routed",
            "covering 90 sites, with failures not routed",
        )
    )

    [drift] = reanchor_many(OLD_CV, new_cv, [anchor_for(OLD_CV, QUOTE)])

    assert drift.material
    assert "figures" in drift.reason
    assert "negation" in drift.reason


def test_anchor_without_a_span_falls_back_to_the_quote():
    new_cv = "## Summary\n\nBuilds data platforms for regulated industries.\n\n" + OLD_CV

    [drift] = reanchor_many(OLD_CV, new_cv, [Anchor(quote=QUOTE, char_start=None, char_end=None)])

    assert drift.found
    assert drift.text == QUOTE
    assert not drift.changed


def test_anchor_with_an_empty_quote_and_no_span_is_not_located():
    [drift] = reanchor_many(OLD_CV, OLD_CV, [Anchor(quote="", char_start=None, char_end=None)])

    assert not drift.found
    assert drift.text == ""


def test_empty_new_document_finds_nothing():
    [drift] = reanchor_many(OLD_CV, "", [anchor_for(OLD_CV, QUOTE)])

    assert not drift.found
    assert drift.text == ""
    assert "gone" in drift.reason


def test_many_anchors_share_one_document_diff(monkeypatch):
    calls = 0
    real_opcodes = anchoring._opcodes

    def counted_opcodes(old_text: str, new_text: str):
        nonlocal calls
        calls += 1
        return real_opcodes(old_text, new_text)

    monkeypatch.setattr(anchoring, "_opcodes", counted_opcodes)
    anchors = [
        anchor_for(OLD_CV, QUOTE),
        anchor_for(OLD_CV, "Owned the nightly clinical load and the definitions behind it"),
    ]

    drifts = reanchor_many(OLD_CV, "## Summary\n\n" + OLD_CV, anchors)

    assert calls == 1
    assert all(drift.found and not drift.changed for drift in drifts)


def test_long_cv_only_refines_the_changed_line():
    lines = [f"- Evidence statement {index} with a distinct value\n" for index in range(2_000)]
    old_cv = "".join(lines)
    quote = lines[1_500].strip()[2:]
    new_cv = old_cv.replace("distinct value\n", "updated value\n", 1)

    [drift] = reanchor_many(old_cv, new_cv, [anchor_for(old_cv, quote)])

    assert drift.found
    assert not drift.changed
