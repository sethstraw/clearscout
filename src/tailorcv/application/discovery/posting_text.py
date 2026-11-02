"""Read what a posting body states, without a database, a network, or a model.

A fact absent from the text stays absent rather than being guessed, and the
fragments a diff compares are drawn from the body's own lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import groupby

from tailorcv.domain.countries import DEFAULT_TABLE
from tailorcv.domain.staged_facts import (
    ClosingDateStatement,
    PostedDateGrade,
    PostedDateStatement,
    PublisherClosingDate,
    PublisherPostedDate,
)
from tailorcv.shared.layout import ITEM_MARKER, plain_layout
from tailorcv.shared.quantities import FIGURE, unit
from tailorcv.shared.text import sanitize, sentences

_LABELLED_FACT = re.compile(
    r"(?im)^\s*(?:[-*\u2022]\s*|\d+\.\s+)?(?:\*\*)?(?P<label>job\s+title|title|position|role|"
    r"(?:job\s+|work\s+)?location(?:s|\(s\))?|residence|residency|work\s+authori[sz]ation|"
    r"employment\s+type|job\s+type|payroll|engagement)(?:\*\*)?\s*(?::|\s-\s)\s*(?:\*\*)?"
    r"(?P<value>[^\n]{1,500})$"
)
# Every written label form reads under the one key the fact readers ask for.
_LABEL_KEYS = (
    (re.compile(r"^(?:(?:job|work) )?location(?:s|\(s\))?$"), "location"),
    (re.compile(r"^work authorisation$"), "work authorization"),
)
_HEADING_TITLE = re.compile(r"(?m)^#\s+([^\n]{1,500})$")
# "US residency is required" and "Residency in Canada is required" name the place a
# person lives right beside the noun.
_RESIDENCY = re.compile(
    r"(?i)\bresiden(?:cy|ce)\s+(?:in\s+(?P<place>(?:the\s+)?[\w.]+(?:\s+[\w.]+){0,3}?)\s+)?"
    r"(?:is\s+)?required\b"
)
# A residency the person completes is medical or pharmacy training, not a home.
_TRAINING_RESIDENCY = re.compile(
    r"(?i)\bcompletion\s+of\b|\bcompleted?\b|\bpgy\s*-?\s*\d|\bprograms?\b|\bfellowship\b"
)
# Words that may stand before "residency in <place>"; any other names a specialty.
_RESIDENCY_LEADS = frozenset(
    {"a", "an", "the", "of", "legal", "permanent", "current", "proof", "valid", "your", "and", "or"}
)
# "Resident" carries a second sense a posting uses freely, for the people an
# employer serves and for a privacy notice. Only the rule sense is a residence rule.
_RESIDENCE = re.compile(
    r"(?i)(?:\bmust\s+(?:currently\s+)?(?:live|reside)\b|\bresidency\s+requirement\b|"
    r"\bmust\s+(?:currently\s+)?be\s+(?:residing|living|located|based)\s+(?:in|within)\b|"
    r"\brequires?\s+(?:you\s+)?to\s+(?:live|reside)\b|"
    r"\b(?:permanent|legal)\s+resident\b|\bmust\s+be\s+an?\s+(?:[\w.]+\s+)?resident\b|"
    r"\bwork\s+authori[sz]ation\b|\bauthori[sz]ed\s+to\s+(?:work|be\s+employed)\b|"
    r"\b(?:eligible|right)\s+to\s+work\s+in\b|"
    r"\b(?:visa|immigration|employment|work|h-?1b)\s+sponsorship\b|\bimmigration\s+support\b|"
    r"\b(?:unable|not\s+able|cannot|can't|do\s+not|don't|does\s+not|doesn't|will\s+not|won't)"
    # "cannot sponsor their benefits plan" and "don't sponsor events" name another sponsor.
    r"\s+(?:to\s+)?(?:provide\s+)?sponsor(?:ship)?(?=\s*(?:[.;,)]|$)|\s+(?:any\s+)?(?:visas?|work"
    r"|employment|immigration|h-?1b|applicants?|candidates?|you\b|anyone|at\s+this\s+time"
    r"|for\s+(?:this|employment|visas?|work|immigration)))|"
    r"\bsponsorship\s+(?:for\s+this\s+(?:role|position|job)\s+)?(?:is|will|are)\b)"
)
# An equal-opportunity notice names work authorization among the laws the employer obeys;
# "must complete employment eligibility verification" beside a rule is still the rule.
_COMPLIANCE_NOTICE = re.compile(
    r"(?i)\bcompl(?:y|ies)\s+with\b|\blaws?\s+governing\b|\be-?verify\b"
    r"|\bimmigration\s+reform\s+and\s+control\s+act\b"
    r"|\bdocumentation\s+of\s+identity\s+and\s+work\s+authori[sz]ation\b"
)
_ELIGIBILITY_VERIFICATION = re.compile(r"(?i)\bemployment\s+eligibility\s+verification\b")
_REQUIREMENT_MODAL = re.compile(r"(?i)\b(?:must|require[ds]?|eligible)\b")
# An over-long sentence is one ``sentences`` kept whole at an abbreviation, so only a
# semicolon or an opener ends a clause in it: "in the U.S. At this time" splits and
# "in the U.S. State of Texas" goes on.
_CLAUSE_END = re.compile(
    r"(?<=;)\s+|(?<=\.)\s+(?=(?:At|In|For|As|Please|Currently|Unfortunately|However)\b)"
)
_EMPLOYMENT = (
    (re.compile(r"(?i)\bfull[- ]?time\b"), "full-time"),
    (re.compile(r"(?i)\bpart[- ]?time\b"), "part-time"),
    (re.compile(r"(?i)\bintern(?:s|ship)?\b"), "internship"),
    (re.compile(r"(?i)\btemp(?:orary)?\b(?!\s+(?:housing|accommodation|lodging))"), "temporary"),
    (re.compile(r"(?i)\bpermanent\b(?!\s+resident)"), "permanent"),
    (re.compile(r"(?i)\bcontract\b(?!\s+(?:management|negotiation|review|law))"), "contract"),
)
_PAYROLL = (
    # "Form W-2" is the tax document, not the payroll arrangement.
    (re.compile(r"(?i)(?<!form )\bW[- ]?2\b"), "W-2"),
    (re.compile(r"(?i)\b1099\b"), "1099"),
    (re.compile(r"(?i)\bindependent contractor\b"), "independent contractor"),
    (
        re.compile(r"(?i)\bemployee\b(?!\s+(?:benefits?|discounts?|assistance|referrals?))"),
        "employee",
    ),
)

# A title, a place, and a residence rule are short statements. A longer candidate,
# or one running past its own sentence or clause, is absent.
_MAX_STATEMENT = 200

_COMPENSATION = re.compile(r"(?i)(?:\$|\b(?:cad|usd|salary|compensation|pay range)\b)")
_LOCATION = re.compile(r"(?i)\b(?:location|remote|hybrid|onsite|on-site|canada|united states)\b")
# "US" and "UK" are codes only in capitals; lower case "us" is the pronoun.
_COUNTRY = (
    r"(?:U\.S\.(?:A\.)?|USA|(?-i:US|UK)|United States(?: of America)?|Canada"
    r"|United Kingdom|U\.K\.)"
)
# "U.S." ends in a period, so the place ends where no word character follows.
_COUNTRY_REMOTE_LOCATION = re.compile(
    rf"(?i)\b(?P<location>{_COUNTRY}\s*[-/]?\s*remote"
    rf"|remote\s*(?:[-,(:/]|\bin\s+(?:the\s+)?)\s*{_COUNTRY}\)?)(?!\w)"
)
# "remote in the US or Canada" and "US/UK remote" name two countries; the phrase
# around one of them is not the posting's location. A board's "Canada Remote, Canada"
# repeats one country and still names it alone.
_COUNTRY_NAME = re.compile(rf"(?i){_COUNTRY}")
_JOINER = r"(?:,|/|&|\bor\b|\band\b)"
_JOINED_COUNTRY_AFTER = re.compile(
    rf"(?i)[^\S\n]*{_JOINER}[^\S\n]*(?:the\s+)?(?P<country>{_COUNTRY})(?!\w)"
)
# "US Remote, UK Remote" joins a second country-remote phrase on after the first.
_JOINED_BEFORE = re.compile(
    rf"(?i)(?P<country>{_COUNTRY})(?:[^\S\n]*[-/]?[^\S\n]*remote)?[^\S\n]*{_JOINER}?[^\S\n]*$"
)
_ARRANGEMENT_ONLY = re.compile(
    r"(?i)(?:fully[- ]?)?(?:remote|hybrid|on[- ]?site|in[- ](?:person|office))\.?"
)
# "Burlington, MA working 3 days in office, U.S. residents only" states its place first.
_PLACE_CLAUSE_END = re.compile(
    rf"(?i)[\s,;:(-]+(?=working\b|(?:{FIGURE})\s*(?:-\s*)?{unit('day')}"
    r"|[\w.]+\s+(?:citizens?|residents?)\s+only\b)"
)
_REQUIREMENT = re.compile(r"(?i)\b(?:required|requirement|qualification|preferred|must have)\b")

# A posting may repeat a short phrase; twenty characters is the length at which a
# phrase written twice in a row is a duplicated field rather than English.
_MIN_DOUBLED_PHRASE = 20
_MINIMUM_CARDS = 5


@dataclass(frozen=True)
class PostingFacts:
    """Facts stated by the latest posting body or its board listing."""

    title: str = ""
    location: str = ""
    residence_rule: str = ""
    employment_type: str = ""
    payroll_arrangement: str = ""


# A funding round, an enrollment window or a deal also "has closed"; only the
# posting itself, or the "it" of a reader's own closure note, closes a posting.
_POSTING_SUBJECT = (
    r"(?:this|the)\s+(?:job(?:\s+(?:posting|opening|listing))?|position|posting|role"
    r"|requisition|listing|opening|vacancy|opportunity)"
    r"(?:\s+you\s+(?:are\s+looking\s+for|requested))?"
)
# "closed to agencies" and "filled by an internal process" describe an open posting.
_CLOSED = r"(?:closed(?![\w-]|\s+(?:to|for)\b)|expired|filled(?![\w-]|\s+(?:by|on|in)\b))"
# "it is closed" and "the position is filled" also describe duties ("make sure it is
# filled correctly"); the perfect "has been filled", or "is" ending its clause, reports one.
_SUBJECT_CLOSURE = re.compile(
    rf"\b(?:{_POSTING_SUBJECT}|it)\s+has\s+(?:now\s+)?(?:been\s+)?{_CLOSED}"
    rf"|\b{_POSTING_SUBJECT}\s+is\s+(?:now\s+{_CLOSED}|{_CLOSED}(?=\s*(?:[.!;]|$)))"
    rf"|\b{_POSTING_SUBJECT}\s+(?:has|is)\s+(?:now\s+)?no\s+longer\s+available\b"
    rf"|\b{_POSTING_SUBJECT}\s+is\s+(?:no\s+longer|not)\s+(?:currently\s+)?"
    r"(?:accepting|open\s+for)\s+(?:new\s+)?applications\b(?:\s+at\s+this\s+time\b)?(?=\s*(?:[.!;]|$))"
    r"|\bapplications\s+(?:have|are)\s+(?:now\s+)?closed\b(?!\s+(?:to|for)\b)",
    re.IGNORECASE,
)
# A reader's own note and a board's fixed phrase close whatever precedes them: the
# note starts with a URL whose slug may hold "after" or "when".
_FIXED_CLOSURE = re.compile(
    r"returned http 404|returned http 410|posting may have closed"
    r"|\bno\s+longer\s+accepting\s+applications\b"
    # Open postings refuse agencies in this sentence too ("... for this position
    # from recruitment agencies."), so only the sentence ending here closes.
    r"|not currently accepting applications for this position\."
    r"|this listing has expired|job is no longer available|the job you requested was not found"
    r"|we cannot find this position|\bjob not found\b(?!\s+in\b)",
    re.IGNORECASE,
)


# "Open until such time as the position is filled" and "we may close early if the role
# is filled" anticipate a closure an open posting has not reached.
_ANTICIPATED = re.compile(
    r"\b(?:until|till|once|when|whenever|unless|if|after|before|should|in\s+case|as\s+soon\s+as"
    r"|by\s+the\s+time|in\s+the\s+event|provided|whether)\b",
    re.IGNORECASE,
)
_CLAUSE_START = re.compile(r"[.;:!?](?=\s|$)")

# A label opening its own line names the posting's close; one after another word
# ("Contract End Date", "Program Application Deadline") names something else.
_POSTING_END = re.compile(
    r"(?im)^[ \t]*(?:[-*\u2022][ \t]*|\d+\.[ \t]+)?(?:\*\*)?"
    r"(?:(?:job[ \t]+)?posting[ \t]+(?:end|close|closing|expiry|expiration)[ \t]+date"
    r"|clos(?:e|ing)[ \t]+date|application[ \t]+deadline|apply[ \t]+by"
    r"|applications?[ \t]+close[sd]?)"
    r"[ \t:*-]*(?:\n[ \t*]*){0,2}(?P<value>[^\n]*)"
)
# Only "is" or "will be" places the role; "can be based in" and "is not based in" do not.
# The subject opens its sentence, line, list item or clause, so "the country the
# position is located in" never reads as a placement.
_ROLE_PLACE = re.compile(
    r"(?is)(?:(?:^|\n)\s*(?:(?:[-*+\u2022\u2023\u25cf\u25aa\u2013\u2014]|\d+[.)]|[a-z]\))\s+)?"
    r"|[,;:(]\s*|\b(?:and|but|so)\s+)"
    rf"(?P<subject>{_POSTING_SUBJECT}\s+(?:is|will\s+be)"
    r"|this\s+is\s+an?\s+(?:[\w-]+\s+){0,3}?(?:role|position|job))"
    r"\s+(?:based\s+(?:in|out\s+of)|located\s+in)\s+(?P<place>.+)"
)
# "a fully remote position based in London" names a nominal base, not where the work happens.
_REMOTE_SUBJECT = re.compile(r"(?i)\b(?:remote|home|distributed|virtual)\b")
# A remote offer or a hedge anywhere after the place leaves the role's place open.
_PLACE_HEDGE = re.compile(
    r"(?i)\b(?:remote(?:ly)?|anywhere|home|elsewhere|preferred|ideally|negotiable"
    r"|flexible\s+(?:on|about)\s+(?:location|where)|depending|subject\s+to"
    r"|but|though|although|unless|except)\b"
)
# "our Global Capability Centre in Lakeside, Poland" places the role at the facility's town.
_FACILITY_PLACE = re.compile(
    r"(?i)(?:our|the)\s+(?:[\w-]+\s+){0,4}?"
    r"(?:office|centre|center|site|hub|campus|facility|headquarters|hq)\s+in\s+(?P<place>.+)"
)
_PLACE_TRAILER = re.compile(r"(?i),?\s+(?:and\s+(?:hybrid|remote)|with|where)\b")
# Joined places, a remote offer, and "our office" name no single place the role sits in.
_NOT_ONE_PLACE = re.compile(
    r"(?i)\b(?:or|and|remote(?:ly)?|our|its|their|one\s+of|office|headquarters|hq|campus)\b|[/&]"
)
_MONTH_NAMES = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTHS = {
    name: number for number, full in enumerate(_MONTH_NAMES, start=1) for name in (full, full[:3])
} | {"sept": 9}
_WEEKDAY = r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?[ \t]+)?"
_ISO_DAY = re.compile(r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})(?!\d)")
_DAY_FIRST = re.compile(
    rf"(?i){_WEEKDAY}(?P<day>\d{{1,2}})[ \t-](?P<month>[a-z]{{3,9}})\.?,?"
    r"[ \t-](?P<year>\d{4})(?!\d)"
)
_MONTH_FIRST = re.compile(
    rf"(?i){_WEEKDAY}(?P<month>[a-z]{{3,9}})\.?[ \t]+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?"
    r"[ \t]+(?P<year>\d{4})(?!\d)"
)
_A_YEAR = re.compile(r"(?<!\d)\d{4}(?!\d)")
_EPOCH_MILLISECONDS = re.compile(r"\d{13}")
_UNPADDED_ISO_DAY = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?=$|[Tt ])")
_COLONLESS_OFFSET = re.compile(r"(T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?[+-]\d{2})(\d{2})$")
# Java's Date.toString: "Thu Sep 17 02:00:00 UTC 2026".
_JAVA_DATE = re.compile(
    r"[A-Z][a-z]{2} (?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2}) (?P<time>\d{2}:\d{2}:\d{2}) "
    r"(?P<zone>[A-Z]{2,5}) (?P<year>\d{4})"
)


def closure_signal(message: str) -> bool:
    """Report whether an ingest failure or a page states that the posting itself is gone."""
    text = " ".join(sanitize(message).split())
    if _FIXED_CLOSURE.search(text):
        return True
    return any(not _anticipated(text, found.start()) for found in _SUBJECT_CLOSURE.finditer(text))


def _anticipated(text: str, end: int) -> bool:
    """Return whether the clause holding a closure phrase only anticipates that closure."""
    starts = [mark.end() for mark in _CLAUSE_START.finditer(text, 0, end)]
    return _ANTICIPATED.search(text, starts[-1] if starts else 0, end) is not None


def _opens_with_a_doubled_phrase(line: str) -> bool:
    """Report whether a line opens with one phrase written twice back to back."""
    longest = min(len(line) // 2, _MAX_STATEMENT)
    return any(
        line[:size] == line[size : size * 2] for size in range(_MIN_DOUBLED_PHRASE, longest + 1)
    )


def lists_many_postings(text: str) -> bool:
    """Report whether a body is a page of job cards rather than the body of one posting.

    A card extracts as its own title twice, either on two lines in a row or run
    together inside one line, and then its employer.
    """
    lines = [
        collapsed
        for line in sanitize(text).splitlines()
        if len(collapsed := " ".join(line.split())) >= 2 * _MIN_DOUBLED_PHRASE
    ]
    doubled = sum(1 for line in lines if _opens_with_a_doubled_phrase(line))
    # Exactly two, because a longer run of one line is boilerplate rather than a card.
    paired = sum(1 for _line, run in groupby(lines) if len(list(run)) == 2)
    # A paired title takes two of the lines counted, a doubled one takes a single line.
    return doubled + paired >= _MINIMUM_CARDS and (doubled + 2 * paired) * 2 > len(lines)


def publisher_closing_date(
    text: str,
    *,
    valid_through: str = "",
    source_url: str = "",
    artifact_id: str | None = None,
    observed_at: datetime | None = None,
) -> PublisherClosingDate:
    """Retain publisher closing statements without inferring posting closure."""
    statements = []
    if valid_through.strip():
        statements.append(_closing_statement(valid_through, valid_through.strip(), "valid_through"))
    for found in _POSTING_END.finditer(text):
        statements.append(
            _closing_statement(found.group().strip(), found["value"].strip(), "body_label")
        )
    return PublisherClosingDate(
        statements=tuple(statements),
        source_url=source_url,
        artifact_id=artifact_id,
        observed_at=observed_at,
    )


def _closing_statement(wording: str, value: str, basis: str) -> ClosingDateStatement:
    if re.fullmatch(r"(?i)(?:open\s+)?until\s+filled|rolling(?:\s+applications)?", value):
        return ClosingDateStatement(wording=wording, basis=basis)
    normalized, precision, zone, unclear = _stated_date(value)
    if normalized is None:
        return ClosingDateStatement(
            wording, basis, clarification=unclear or "Publisher closing date needs clarification."
        )
    return ClosingDateStatement(wording, basis, normalized, precision, zone)


def _stated_date(value: str) -> tuple[str | None, str | None, str | None, str]:
    """Return a value's normalized date, its precision and stated zone, and why it is unclear."""
    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})?",
        value,
    ):
        offset = re.search(r"[+-](\d{2}):(\d{2})$", value)
        if offset and (int(offset[1]) > 23 or int(offset[2]) > 59):
            return None, None, None, "Invalid publisher timezone."
        try:
            stamp = datetime.fromisoformat(value.replace("z", "Z"))
        except ValueError:
            return None, None, None, "Invalid publisher timestamp."
        zone = "+00:00" if value[-1:] in {"Z", "z"} else value[-6:] if stamp.tzinfo else None
        return stamp.isoformat(), "timestamp", zone, ""
    for pattern in (_ISO_DAY, _DAY_FIRST, _MONTH_FIRST):
        found = pattern.match(value)
        if found is None or value[found.end() :].strip(" \t.*"):
            continue
        day = _leading_date(value)
        if day is not None:
            return day.isoformat(), "date", None, ""
    return None, None, None, ""


def publisher_posted_date(
    value: str, *, basis: str, grade: PostedDateGrade
) -> PublisherPostedDate | None:
    """Return the posted date one publisher field states, or None when it states no dated day.

    Relative text such as "Posted 30+ Days Ago" names no year and is never a date.
    """
    wording = value.strip()
    if not _EPOCH_MILLISECONDS.fullmatch(wording) and not _A_YEAR.search(wording):
        return None
    normalized, precision, zone, unclear = _stated_date(_iso_form(wording))
    return PublisherPostedDate.stated(
        PostedDateStatement(
            wording,
            basis,
            grade,
            normalized,
            precision,
            zone,
            "" if normalized else unclear or "Publisher posted date needs clarification.",
        )
    )


def _iso_form(wording: str) -> str:
    """Rewrite the posted-date forms boards emit beyond ISO 8601 into the ISO the parser reads."""
    if _EPOCH_MILLISECONDS.fullmatch(wording):
        return datetime.fromtimestamp(int(wording) / 1000, UTC).isoformat()
    found = _JAVA_DATE.fullmatch(wording)
    if found and (number := _MONTHS.get(found["month"].casefold())):
        # Another zone abbreviation is ambiguous, so the time stays naive.
        zone = "+00:00" if found["zone"] in {"UTC", "GMT"} else ""
        return f"{found['year']}-{number:02}-{int(found['day']):02}T{found['time']}{zone}"
    padded = _UNPADDED_ISO_DAY.sub(
        lambda day: f"{day['year']}-{int(day['month']):02}-{int(day['day']):02}", wording
    )
    return _COLONLESS_OFFSET.sub(r"\1:\2", padded)


def _leading_date(value: str) -> date | None:
    """Return the calendar date a value opens with, or None when it opens with none."""
    for pattern in (_ISO_DAY, _DAY_FIRST, _MONTH_FIRST):
        found = pattern.match(value)
        if found is None:
            continue
        month = found["month"]
        number = int(month) if month.isdigit() else _MONTHS.get(month.casefold())
        if number is None:
            return None
        try:
            return date(int(found["year"]), number, int(found["day"]))
        except ValueError:
            return None
    return None


def _statement(value: str) -> str:
    """Return a value only when it is one short statement rather than a run of prose."""
    if len(value) > _MAX_STATEMENT or len(sentences(value)) > 1:
        return ""
    return value


def _states_residency_requirement(text: str) -> bool:
    """Return whether ``text`` requires residency in a place it names beside the noun."""
    text = sanitize(text)
    if _TRAINING_RESIDENCY.search(text):
        return False
    for found in _RESIDENCY.finditer(text):
        before = text[: found.start()].split()[-3:]
        named_before = _ends_in_place(" ".join(before))
        if found["place"] is None:
            if named_before:
                return True
            continue
        lead = before[-1].casefold().strip(".,;:()") if before else ""
        # "Psychiatry residency in the US" names a specialty before the noun.
        if (not lead or lead in _RESIDENCY_LEADS or named_before) and DEFAULT_TABLE.names_in(
            found["place"]
        ):
            return True
    return False


def _ends_in_place(text: str) -> bool:
    """Return whether ``text`` ends in a country or region name, as "a US" or "the UK" do."""
    trimmed = text.rstrip(",;:()")
    stem = trimmed.rstrip(".")
    return any(end >= len(stem) for (_start, end), _ in DEFAULT_TABLE.spans_in(trimmed))


def _compliance_notice(sentence: str) -> bool:
    """Return whether ``sentence`` frames work authorization as a law the employer obeys."""
    return bool(
        _COMPLIANCE_NOTICE.search(sentence)
        or (_ELIGIBILITY_VERIFICATION.search(sentence) and not _REQUIREMENT_MODAL.search(sentence))
    )


def _states_residence_rule(sentence: str) -> bool:
    return (
        len(sentence) <= _MAX_STATEMENT
        and not _compliance_notice(sentence)
        and bool(_RESIDENCE.search(sentence) or _states_residency_requirement(sentence))
    )


def _residence_statement(line: str) -> str:
    """Return the first sentence, or clause of an over-long sentence, that states the rule."""
    pieces = (
        piece
        for sentence in sentences(line)
        for piece in (
            [sentence] if len(sentence) <= _MAX_STATEMENT else _CLAUSE_END.split(sentence)
        )
    )
    return next((piece for piece in pieces if _states_residence_rule(piece)), "")


def _label_key(label: str) -> str:
    """Return the key a written label reads under."""
    key = " ".join(label.casefold().split())
    return next((name for form, name in _LABEL_KEYS if form.match(key)), key)


def _labelled_facts(text: str) -> dict[str, str]:
    """Map each label a posting states to its collapsed value."""
    labelled: dict[str, str] = {}
    for match in _LABELLED_FACT.finditer(text):
        key = _label_key(match.group("label"))
        value = " ".join(match.group("value").split())
        if key == "location":
            value = _stated_place(value)
        if value:
            labelled[key] = value
    return labelled


def _stated_title(text: str, hint: str, labelled: dict[str, str]) -> str:
    """Take the hint, else a labelled title, else the body's first heading."""
    title = hint.strip()
    if not title:
        title = next(
            (
                labelled[key]
                for key in ("job title", "title", "position", "role")
                if key in labelled
            ),
            "",
        )
    if title:
        return title
    heading = _HEADING_TITLE.search(text)
    return " ".join(heading.group(1).split()) if heading else ""


def _stated_residence_rule(lines: list[str], labelled: dict[str, str]) -> str:
    """Take a labelled residence rule, else the first line that states one."""
    labelled_rule = _statement(
        next(
            (
                value
                for key in ("residence", "residency", "work authorization")
                if (value := labelled.get(key))
            ),
            "",
        )
    )
    if labelled_rule:
        return labelled_rule
    return next((statement for line in lines if (statement := _residence_statement(line))), "")


def _categorized_fact(
    text: str,
    labelled: dict[str, str],
    keys: tuple[str, ...],
    vocabulary: tuple[tuple[re.Pattern[str], str], ...],
) -> str:
    """Take a labelled value, else the first canonical term the body uses."""
    stated = next((labelled[key] for key in keys if labelled.get(key)), "")
    if stated:
        return stated
    return next((canonical for pattern, canonical in vocabulary if pattern.search(text)), "")


def _location_statement(text: str, hint: str, labelled: dict[str, str]) -> str:
    location = _statement(hint.strip() or labelled.get("location", ""))
    if location:
        return location
    country_remote = next(
        (found for found in _COUNTRY_REMOTE_LOCATION.finditer(text) if _one_country(text, found)),
        None,
    )
    return _statement(country_remote.group("location")) if country_remote else ""


def _one_country(text: str, found: re.Match[str]) -> bool:
    """Return whether a country-remote phrase names its country alone, with no other joined on."""
    named = _COUNTRY_NAME.search(found.group())
    own = DEFAULT_TABLE.named_in(named.group()) if named else ""
    joined = (
        _JOINED_COUNTRY_AFTER.match(text, found.end()),
        _JOINED_BEFORE.search(text, 0, found.start()),
    )
    return all(DEFAULT_TABLE.named_in(other["country"]) == own for other in joined if other)


def _stated_place(value: str) -> str:
    """Return a labelled location's place, or nothing when it names only an arrangement."""
    if _ARRANGEMENT_ONLY.fullmatch(value):
        return ""
    place = _PLACE_CLAUSE_END.split(value, maxsplit=1)[0].strip()
    # "Florence, SC (Hybrid, 3+ days onsite)" leaves its parenthesis open once cut.
    if place.count("(") > place.count(")"):
        place = place[: place.rfind("(")].rstrip(" ,;:-")
    # "Remote - Canadian residents only" keeps its place in the clause the cut would drop.
    return value if _ARRANGEMENT_ONLY.fullmatch(place) else place


def role_based_place(text: str) -> str:
    """Return the one place a sentence says the role itself is based in, else nothing."""
    for sentence in sentences(sanitize(text)):
        flat = re.sub(r"[^\S\n]+", " ", sentence)
        found = _ROLE_PLACE.search(flat)
        if found is None:
            continue
        if (
            _anticipated(flat, found.start("subject"))
            or _REMOTE_SUBJECT.search(found["subject"])
            or _PLACE_HEDGE.search(found["place"])
        ):
            return ""
        place = _PLACE_TRAILER.split(" ".join(found["place"].split()), maxsplit=1)[0]
        facility = _FACILITY_PLACE.fullmatch(place)
        if facility is not None:
            place = facility["place"]
        place = _stated_place(place.strip(" .,;:!?"))
        if not place or _ARRANGEMENT_ONLY.fullmatch(place) or _NOT_ONE_PLACE.search(place):
            return ""
        return _statement(place)
    return ""


def posting_facts(text: str, *, title_hint: str = "", location_hint: str = "") -> PostingFacts:
    """Read only explicit facts; an absent fact stays absent instead of being copied."""
    text = sanitize(text)
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    labelled = _labelled_facts(text)
    employment_type = _categorized_fact(
        text, labelled, ("employment type", "job type"), _EMPLOYMENT
    )
    payroll_arrangement = _categorized_fact(text, labelled, ("payroll", "engagement"), _PAYROLL)
    return PostingFacts(
        title=_statement(_stated_title(text, title_hint, labelled)),
        location=_location_statement(text, location_hint, labelled),
        residence_rule=_stated_residence_rule(lines, labelled),
        employment_type=employment_type[:120],
        payroll_arrangement=payroll_arrangement[:120],
    )


def posting_fragments(text: str) -> dict[str, list[str]]:
    """Group the body's own lines under the topics a posting diff compares."""
    # Compared in the canonical layout and without item markers, so a relaid body is no change.
    lines = [
        line[marker.end() :] if (marker := ITEM_MARKER.match(line)) else line
        for line in plain_layout(text).splitlines()
        if line
    ]
    return {
        "location": [line for line in lines if _LOCATION.search(line)],
        "compensation": [line for line in lines if _COMPENSATION.search(line)],
        "requirements": [line for line in lines if _REQUIREMENT.search(line)],
    }
