"""WCAG AA contrast check for every text/background pair defined in the theme stylesheet."""
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).parent.parent / "tracker" / "web" / "static" / "style.css").read_text(encoding="utf-8")
VARS = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", CSS))


def lum(hex_):
    r, g, b = (int(hex_[i : i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT_ON = [
    ("text", "bg"), ("text", "paper"), ("text", "raised"),
    ("text-2", "bg"), ("text-2", "paper"), ("text-2", "raised"),
    ("accent-text", "bg"), ("accent-text", "paper"), ("accent-text", "raised"),
    ("on-accent", "accent"), ("on-accent", "accent-hover"),
    ("on-badge-applied", "badge-applied"), ("on-badge-interviewing", "badge-interviewing"),
    ("on-badge-offer", "badge-offer"), ("on-badge-rejected", "badge-rejected"),
    ("on-badge-withdrawn", "badge-withdrawn"), ("on-badge-amber", "badge-amber"),
    ("on-danger", "danger"), ("badge-amber", "bg"), ("badge-amber", "paper"),
]


@pytest.mark.parametrize("fg,bg", TEXT_ON)
def test_contrast_aa(fg, bg):
    assert ratio(VARS[fg], VARS[bg]) >= 4.5, f"{fg} on {bg}: {ratio(VARS[fg], VARS[bg]):.2f}"


def test_no_hardcoded_colours_outside_root():
    outside = CSS.split("}", 1)[1]  # everything after the :root block
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", outside)


def test_no_hardcoded_colours_in_templates():
    for f in (Path(__file__).parent.parent / "tracker" / "web" / "templates").glob("*.html"):
        assert not re.search(r"#[0-9a-fA-F]{6}\b|style=\"[^\"]*color", f.read_text(encoding="utf-8")), f.name
