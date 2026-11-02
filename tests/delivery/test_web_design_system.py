"""Design system invariants that are decidable from the checked-in web sources.

File-content assertions, in the same spirit as the audit scripts: they render
nothing and claim nothing about how a screen looks. The color check lives here
rather than in vitest because reading a source file from a test needs Node type
definitions the web package does not carry.
"""

from __future__ import annotations

import re

import pytest

from tests.support.suite import REPO_ROOT

WEB = REPO_ROOT / "web" / "src"
BADGE = WEB / "components" / "statuses.ts"
RESPONSIVE = WEB / "styles" / "responsive.css"


def _stylesheet() -> str:
    """Every rule the application ships, in the order styles.css imports them."""
    index = (WEB / "styles.css").read_text(encoding="utf-8")
    names = re.findall(r'@import "\./styles/([\w.-]+)"', index)
    if not names:
        raise AssertionError("styles.css imports nothing; the stylesheet moved")
    return "\n".join((WEB / "styles" / name).read_text(encoding="utf-8") for name in names)


# WCAG 2.1 AA for normal-size text. Status badges render well under 18.66px, so
# the large-text allowance of 3.0 never applies to them.
AA_NORMAL = 4.5

# The badge tint is a harsher background than the plain surface, and testing
# at the darkest mix covers the lighter ones.
BADGE_TINT_PERCENT = 12


def _root_block() -> str:
    """The light theme custom property block."""
    text = _stylesheet()
    start = text.index(":root {")
    return text[start : text.index("\n}", start)]


def _token(name: str) -> str:
    match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}});", _root_block())
    assert match is not None, f"--{name} is not declared in the light theme :root block"
    return match.group(1)


def _channels(value: str) -> tuple[int, int, int]:
    raw = value.lstrip("#")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def _relative_luminance(value: str) -> float:
    def linear(channel: int) -> float:
        srgb = channel / 255
        return srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(c) for c in _channels(value))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground: str, background: str) -> float:
    first = _relative_luminance(foreground)
    second = _relative_luminance(background)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def _mix(color: str, percent: int, base: str) -> str:
    """Reproduce CSS color-mix(in srgb, color percent%, base)."""
    fraction = percent / 100
    blended = (
        round(c * fraction + b * (1 - fraction))
        for c, b in zip(_channels(color), _channels(base), strict=True)
    )
    return "#" + "".join(f"{channel:02x}" for channel in blended)


# Every token here is used as text somewhere in the light theme.
TEXT_TOKENS = ["teal", "amber", "blue", "danger", "ink-soft", "accent-ink"]

# These are also painted as solid fills, under white text.
FILL_TOKENS = ["teal", "amber", "blue", "danger"]


def test_mobile_settings_grids_override_the_specific_desktop_rules() -> None:
    text = RESPONSIVE.read_text(encoding="utf-8")
    mobile = text.split("@media (max-width: 760px) {", 1)[1].split(
        "@media (prefers-reduced-motion: reduce)", 1
    )[0]
    single_column = {
        selector.strip()
        for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", mobile)
        if "grid-template-columns: 1fr;" in declarations
        for selector in selectors.split(",")
    }

    expected = {".panel-body.settings-grid", ".panel-body .record-list .settings-grid"}
    assert expected <= single_column, (
        f"specific desktop grids without a mobile override: {sorted(expected - single_column)}"
    )


@pytest.mark.parametrize("name", TEXT_TOKENS)
def test_light_theme_text_colour_meets_aa(name: str) -> None:
    value = _token(name)
    surface = _token("surface")
    for background, label in (
        (surface, "--surface"),
        (_token("surface-muted"), "--surface-muted"),
        (_mix(value, BADGE_TINT_PERCENT, surface), "its own badge tint"),
    ):
        ratio = _contrast(value, background)
        assert ratio >= AA_NORMAL, (
            f"--{name} ({value}) is {ratio:.2f} on {label}, needs {AA_NORMAL}"
        )


@pytest.mark.parametrize("name", FILL_TOKENS)
def test_white_stays_legible_on_solid_fills(name: str) -> None:
    value = _token(name)
    ratio = _contrast("#ffffff", value)
    assert ratio >= AA_NORMAL, f"white on --{name} ({value}) is {ratio:.2f}, needs {AA_NORMAL}"


def _statuses_block() -> str:
    """The status vocabulary definitions."""
    return BADGE.read_text(encoding="utf-8")


def _status_tones() -> dict[str, str]:
    block = _statuses_block()
    entries = dict(re.findall(r'(\w+):\s*\{\s*label:[^}]*?tone:\s*"(\w+)"', block))
    assert entries, "no status entries were found in statuses.ts"
    return entries


def test_status_tones_and_stylesheet_cannot_drift() -> None:
    """Every status value with a label has a tone class, and no tone is unused."""
    used = set(_status_tones().values())
    defined = set(re.findall(r"\.status--tone-(\w+)\s*\{", _stylesheet()))

    undefined = sorted(used - defined)
    assert not undefined, f"tones used by a status with no class in styles.css: {undefined}"

    unused = sorted(defined - used)
    assert not unused, f"tone classes in styles.css that no status uses: {unused}"
