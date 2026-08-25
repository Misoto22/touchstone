"""Concern briefs stay technology-neutral; the stack arrives as context."""

from __future__ import annotations

import re
from importlib.resources import files

import pytest

from touchstone.config import LoopConfig

CONCERNS = ("hardcode", "naming", "error-handling", "test-coverage")

#: Naming a technology inside a brief is what turns four files into
#: fifty-four. The Profile supplies the stack; the brief supplies the concern.
_TECHNOLOGIES = re.compile(
    r"\b(Python|Rust|Django|FastAPI|TypeScript|JavaScript|React|Next\.js|C#|\.NET|Cargo|"
    r"npm|pnpm|pytest|clippy)\b"
)


def _text(name: str) -> str:
    return (
        files("touchstone.resources").joinpath("briefs", f"{name}.md").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("concern", CONCERNS)
def test_every_concern_brief_is_packaged(concern: str) -> None:
    assert len(_text(concern)) > 400


@pytest.mark.parametrize("concern", CONCERNS)
def test_a_concern_brief_names_no_technology(concern: str) -> None:
    found = _TECHNOLOGIES.findall(_text(concern))

    assert not found, f"{concern} names {sorted(set(found))}; that belongs in a Profile"


@pytest.mark.parametrize("concern", CONCERNS)
def test_no_unfilled_placeholder_ever_reaches_a_session(concern: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    loop = LoopConfig(
        name=concern,
        brief=f"builtin:{concern}",
        label="touchstone:audit",
        config_dir=tmp_path,
    )

    assert "$" not in loop.prompt()


def test_declared_naming_rules_reach_the_brief(tmp_path) -> None:  # type: ignore[no-untyped-def]
    loop = LoopConfig(
        name="naming",
        brief="builtin:naming",
        label="touchstone:naming",
        config_dir=tmp_path,
        context=(("naming", "functions are snake_case; types are PascalCase"),),
    )

    assert "functions are snake_case" in loop.prompt()


def test_a_concern_brief_reviews_through_the_shared_reviewer(tmp_path) -> None:  # type: ignore[no-untyped-def]
    loop = LoopConfig(
        name="hardcode",
        brief="builtin:hardcode",
        label="touchstone:audit",
        config_dir=tmp_path,
    )

    assert "independent reviewer" in loop.review_prompt()
