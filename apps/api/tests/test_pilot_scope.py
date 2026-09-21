"""The pilot is 35 institutions, and the PRD still says what it said.

These two things pull in opposite directions and both matter. The *implemented* scope
has to follow the client's decision, or a validator quietly accepts 36 and someone
adds a university nobody chose. The *historical* record has to stay as written, or the
next person cannot tell what was asked for originally and what changed afterwards.

So this module asserts both: current tooling defaults to 35, and the PRD-derived
statements are still there, with the later correction recorded beside them rather than
in place of them.

No database, so it runs everywhere.
"""

from __future__ import annotations

import importlib.util
import re
import tokenize
from pathlib import Path

from app.domains.pilot.official_sources import (
    ADDITIONAL_COLUMN,
    COLUMN_CATEGORIES,
    PILOT_INSTITUTION_COUNT,
)

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
API = REPO / "apps" / "api"


def test_the_implemented_pilot_is_thirty_five() -> None:
    """One constant, so a validator, a report and a CLI cannot drift apart."""
    assert PILOT_INSTITUTION_COUNT == 35


def test_the_cli_defaults_to_the_current_scope() -> None:
    """A stale default is worse than no default: it looks decided.

    Loaded by path rather than imported: `scripts/` is not a package, and importing
    it as one makes mypy see every script under two module names.
    """
    path = API / "scripts/import_official_sources.py"
    spec = importlib.util.spec_from_file_location("_import_official_sources_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    defaults = {action.dest: action.default for action in module.build_parser()._actions}
    assert defaults["expect_institutions"] == PILOT_INSTITUTION_COUNT


def _code_tokens(path: Path) -> list[tokenize.TokenInfo]:
    """Every token of a module except comments and docstrings.

    Comments and docstrings are where the history is recorded -- "the PRD asked for
    at least 36", "there is no similarity threshold here, and here is why". Searching
    them would make these tests fail on the very explanations they protect, and the
    obvious fix would be to delete the explanations.
    """
    with path.open("rb") as handle:
        tokens = list(tokenize.tokenize(handle.readline))

    docstrings: set[int] = set()
    expecting = True  # a module starts able to have one
    for index, token in enumerate(tokens):
        if token.type in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT):
            continue
        if expecting and token.type == tokenize.STRING:
            docstrings.add(index)
        expecting = token.type == tokenize.NAME and token.string in {"def", "class"}
        if expecting:
            # The docstring comes after the signature, so skip to its colon.
            for ahead in range(index, len(tokens)):
                if tokens[ahead].type == tokenize.OP and tokens[ahead].string == ":":
                    break

    return [
        token
        for index, token in enumerate(tokens)
        if token.type not in (tokenize.COMMENT, tokenize.ENCODING) and index not in docstrings
    ]


def _live_modules() -> list[Path]:
    """Current tooling. Migrations are excluded: a migration is history, and its
    prose describes what was true when it was written."""
    return sorted([*(API / "src").rglob("*.py"), *(API / "scripts").rglob("*.py")])


def test_no_live_tool_still_asks_for_thirty_six() -> None:
    """Requirement 20, swept across current tooling rather than one call site.

    A stale default is worse than no default, because it looks decided.
    """
    stale: list[str] = []
    for path in _live_modules():
        lines = path.read_text(encoding="utf-8").splitlines()
        for token in _code_tokens(path):
            # ENDMARKER sits one line past the end of the file.
            row = token.start[0] - 1
            line = lines[row] if 0 <= row < len(lines) else ""
            # A bare literal 36 is a default someone will act on. A column width
            # (`width=36`), a slice (`name[:36]`) and a format spec (`{x:36}`) are all
            # display arithmetic and say nothing about scope.
            is_layout = "width=" in line or "[:36]" in line or ":36}" in line
            literal = token.type == tokenize.NUMBER and token.string == "36" and not is_layout
            # A message telling an operator the pilot is 36 is just as stale.
            message = token.type == tokenize.STRING and re.search(
                r"36 (?:universit|institution)|(?:selected|target) 36", token.string
            )
            if literal or message:
                stale.append(f"{path.relative_to(REPO)}:{token.start[0]}: {line.strip()}")
    assert not stale, "current tooling still refers to a 36-institution pilot:\n" + "\n".join(stale)


def test_the_historical_prd_target_is_not_rewritten() -> None:
    """Requirement 21. The PRD asked for at least 36; that is what it asked for.

    Overwriting it would make the architecture document claim the client always
    wanted 35, and the reason the number moved — their decision, later — would be
    gone. The correction is recorded beside it instead.
    """
    architecture = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "≥36 institutions" in architecture, "the PRD volume target was rewritten"

    adr = (DOCS / "adr" / "0002-single-python-codebase-multiple-runtime-roles.md").read_text(
        encoding="utf-8"
    )
    assert "36 institutions" in adr, "an ADR was edited after the fact"


def test_the_change_of_scope_is_recorded() -> None:
    """And the current number is stated where someone will look for it."""
    architecture = (DOCS / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "35" in architecture
    assert re.search(
        r"35 institutions|fixed .{0,24}35|pilot .{0,16}35", architecture
    ), "ARCHITECTURE.md does not say the pilot is now 35"
    assumptions = (DOCS / "assumptions.md").read_text(encoding="utf-8")
    assert "35" in assumptions


def test_the_source_list_importer_does_no_fuzzy_matching() -> None:
    """Requirement 2, as a structural fact rather than one example.

    The near-miss test proves today's behaviour; this catches the future addition of
    a similarity search "just as a fallback", which is how fuzzy matching gets in.

    Tokenised, so the module's own explanation of why it does not fuzzy-match is not
    itself the failure.
    """
    path = API / "src/app/domains/pilot/official_sources.py"
    forbidden = {
        "similarity",
        "levenshtein",
        "difflib",
        "SequenceMatcher",
        "get_close_matches",
        "word_similarity",
        "startswith",
        "endswith",
    }
    found = sorted(
        {
            token.string
            for token in _code_tokens(path)
            if token.type == tokenize.NAME and token.string in forbidden
        }
    )
    assert not found, (
        f"{found} appear(s) in the official-source importer. Matching is exact: a "
        "threshold loose enough to join 'UCL' to 'University College London' also "
        "joins 'University of Canterbury' to 'Canterbury Christ Church University'."
    )

    sql_fuzz = [
        token.string
        for token in _code_tokens(path)
        if token.type == tokenize.STRING
        and re.search(r"ILIKE|\bLIKE\b|%\s*\|\||similarity\(", token.string, re.IGNORECASE)
    ]
    assert not sql_fuzz, f"fuzzy SQL in the official-source importer: {sql_fuzz}"


def test_every_core_column_has_a_category_and_the_extra_one_does_not() -> None:
    """The mapping table itself, so a renamed heading fails here and not at import."""
    headings = [heading for heading, _ in COLUMN_CATEGORIES]
    assert len(headings) == len(set(headings)) == 10
    assert ADDITIONAL_COLUMN not in headings
    categories = [category for _, category in COLUMN_CATEGORIES]
    assert len(categories) == len(set(categories))
    assert "UNCLASSIFIED" not in categories
