"""A regex that says `\\b` must contain a backslash and a b, not a backspace.

WHY THIS TEST EXISTS
====================
Twice now a pattern has been written into a source file with the two characters `\\b`
replaced by the single control character U+0008, because a tool wrote the file through a
non-raw Python string where `"\\b"` means BACKSPACE. The result is invisible: the file
looks right in an editor, in `grep` output and in a diff, `ruff` and `mypy` both pass,
and the regex compiles. It simply never matches, because no web page contains a
backspace.

The first instance shipped. `language._COORDINATOR` was
`\\b(?:or|and|either|alternatively)\\b|[/;,]` in intent and
`<BS>(?:or|and|either|alternatively)<BS>|[/;,]` in fact, so the word half of the rule had
never run since it was written: only punctuation ever coordinated. It was found by
scanning for control characters after the same mistake was made a second time, not by a
test -- which is why there is now a test.

The second instance was caught before it could ship, in the scope rule's negation guard,
where it would have meant "non-US citizens" silently asserting a United States scope.

The third instance was in **documentation**, not code: a Windows path written through a
generation layer turned `overseas-uni-datahub\\review.cmd` into `overseas-uni-datahub`
followed by U+000D and `eview.cmd`. The rendered Markdown looked almost right, and the
command in it could not have worked. The guard existed by then and did not fire, because
it only scanned Python -- so it now scans Markdown too. A wrong command in a runbook is
the same class of defect as a regex that cannot match: silently inert, and discovered by
whoever needed it.

There is no legitimate reason for a control character to appear in this codebase's
source. Tabs are excluded because Python source may legally contain them, and a file
with mixed indentation is `ruff`'s problem rather than this test's. Markdown is checked
with tabs allowed for the same reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: Every directory whose Python this repository owns.
ROOTS = ("src", "tests", "scripts", "alembic")

#: Control characters that may legitimately appear in source: horizontal tab, and the
#: line terminators, which never survive `splitlines()` anyway.
ALLOWED = frozenset({"\t"})


def _python_files() -> list[Path]:
    base = Path(__file__).resolve().parent.parent
    found: list[Path] = []
    for root in ROOTS:
        directory = base / root
        if directory.is_dir():
            found.extend(
                path for path in sorted(directory.rglob("*.py")) if "__pycache__" not in path.parts
            )
    return found


def _markdown_files() -> list[Path]:
    """The documentation, which is where the third instance of this defect landed.

    A runbook is executable in the sense that matters: somebody copies a line out of it
    and runs it. A control character in the middle of a path makes that line silently
    wrong, which is the same failure as a regex that cannot match.
    """
    repository = Path(__file__).resolve().parents[3]
    found: list[Path] = []
    for directory in (repository / "docs", repository):
        if not directory.is_dir():
            continue
        pattern = "**/*.md" if directory.name == "docs" else "*.md"
        found.extend(sorted(directory.glob(pattern)))
    return sorted(set(found))


def test_there_are_python_files_to_check() -> None:
    """A guard that finds nothing because it looked nowhere is worse than no guard."""
    files = _python_files()
    assert len(files) > 100, f"only found {len(files)} source files; the search is wrong"


def test_there_are_markdown_files_to_check() -> None:
    """Same reasoning: prove the second search actually looks somewhere."""
    files = _markdown_files()
    assert len(files) >= 5, f"only found {len(files)} markdown files; the search is wrong"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.name))
def test_no_source_file_contains_a_stray_control_character(path: Path) -> None:
    """U+0008 in a regex is a `\\b` that was eaten by a non-raw string."""
    offences: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stray = sorted({character for character in line if ord(character) < 32} - ALLOWED)
        if stray:
            names = ", ".join(f"U+{ord(character):04X}" for character in stray)
            offences.append(f"  line {number}: {names}\n    {line!r}")
    assert not offences, (
        f"{path} contains control characters. If this is a regex, the `\\b` word "
        f"boundaries have been replaced by backspaces and the pattern cannot match:\n"
        + "\n".join(offences)
    )


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: str(p.name))
def test_no_markdown_file_contains_a_stray_control_character(path: Path) -> None:
    """A path mangled into a control character makes a runbook command silently wrong."""
    offences: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stray = sorted({character for character in line if ord(character) < 32} - ALLOWED)
        if stray:
            names = ", ".join(f"U+{ord(character):04X}" for character in stray)
            offences.append(f"  line {number}: {names}\n    {line!r}")
    assert not offences, (
        f"{path} contains control characters. A backslash written through a non-raw "
        f"string becomes one of these, and any command on that line is now wrong:\n"
        + "\n".join(offences)
    )
