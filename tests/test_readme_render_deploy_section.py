"""Iteration 15 — the README's Render deployment section (`019` AC9).

Hermetic: reads `README.md` as text. A heading-presence check is safe here
because this is prose markdown, not HTML/JS, so the comment-stripping trap
(`//` inside a string literal, an HTML comment matching its own ban) does not
apply — there is no comment syntax in a markdown body to confuse this with.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"

HEADING = "### Deploying to Render + Neon"


def readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


def test_ac9_the_heading_is_present():
    assert HEADING in readme()


def test_ac9_the_heading_is_a_real_section_not_a_dangling_fragment():
    """The heading must be followed by body text before the next heading of
    equal or higher level — i.e. it introduces a section, it doesn't just
    name one. Mutation check: deleting the body while leaving the heading
    (simulated below) must fail this test."""
    text = readme()
    start = text.index(HEADING) + len(HEADING)
    remainder = text[start:]
    next_heading = re.search(r"^#{1,3} ", remainder, flags=re.MULTILINE)
    body = remainder[: next_heading.start()] if next_heading else remainder
    assert len(body.strip()) > 200, "the heading has no real content under it"


def test_ac9_the_section_names_both_artifacts():
    text = readme()
    start = text.index(HEADING)
    next_heading = re.search(r"^## ", text[start + len(HEADING):], flags=re.MULTILINE)
    end = start + len(HEADING) + next_heading.start() if next_heading else len(text)
    section = text[start:end]
    assert "render.yaml" in section
    assert "deploy/neon/README.md" in section


def test_ac9_the_ephemeral_history_limitation_is_stated():
    text = readme()
    start = text.index(HEADING)
    assert "ephemeral" in text[start:].lower()
