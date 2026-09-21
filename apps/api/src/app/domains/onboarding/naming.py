"""Conservative name normalisation, used only as a match key.

WHAT THIS IS FOR
================
Recognising that row 12 of QS 2027 and row 14 of QS 2028 name the same institution,
so that a re-import continues existing onboarding work instead of duplicating it.

WHAT THIS IS NOT FOR
====================
Deciding that a target institution *is* a particular university. That is identity
resolution, it is a human decision, and it is recorded on
`target_institution.matched_university_id` with an actor and a timestamp.

WHY IT IS DELIBERATELY WEAK
===========================
Aggressive normalisation merges institutions that are genuinely different. The QS
list alone contains ``University of London`` colleges that share most of their
tokens, plus pairs like ``University of Canterbury`` (New Zealand) and
``Canterbury Christ Church University``. Every transformation here is therefore
information-preserving in the sense that matters: it changes only presentation
(case, whitespace, Unicode form, decorative punctuation), never content.

Specifically **not** done, and why:

* No removal of parentheticals. ``University of California, Berkeley (UCB)`` and
  ``University of California, Davis`` differ meaningfully inside and outside the
  parentheses, and stripping them is how institutions in a family get merged.
* No stopword removal. Dropping ``of``/``the`` turns ``The University of Hong Kong``
  and ``Hong Kong University`` -- two different real strings -- into one key without
  anyone deciding they are the same institution.
* No abbreviation expansion. ``UCL`` stays ``ucl``; that it denotes University
  College London is knowledge, not string manipulation.
* No transliteration or translation.
* No fuzzy or phonetic matching. A trigram search exists for *human* lookup
  (``ix_university_name_en_trgm``); it must never assign an identity.

The consequence is accepted on purpose: a genuine rename between list versions
("Essex, University of" becoming "University of Essex") produces a new
`target_institution` rather than being recognised as the same one. That surfaces as
an ADDED_TARGET plus a REMOVED_FROM_NEW_LIST in the difference report, which a human
resolves by matching both targets to the same university. A wrong merge, by
contrast, is silent and corrupts scope. Given the choice, this module errs toward the
visible failure.
"""

from __future__ import annotations

import re
import unicodedata

#: Characters that vary by typist and carry no distinguishing information.
#: Curly quotes and the various dashes are folded to their ASCII equivalents so
#: ``King's`` and ``King’s`` do not become two institutions.
_PUNCTUATION_FOLDING = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
    "−": "-",
    " ": " ",
    "　": " ",
}

#: The same mapping keyed by code point, which is what `str.translate` wants. Built
#: once at import rather than on every call.
_PUNCTUATION_TABLE: dict[int, str] = {
    ord(character): replacement for character, replacement in _PUNCTUATION_FOLDING.items()
}

_WHITESPACE = re.compile(r"\s+")

#: Zero-width and bidirectional control characters. These are invisible, so two
#: names differing only by one look identical to a reviewer -- exactly the case a
#: match key must collapse.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠﻿]")


def normalize_institution_name(raw: str) -> str:
    """Fold a supplied name to a match key.

    Case, Unicode form, whitespace and decorative punctuation only. Raises on a
    value that is empty once folded, because an empty match key would silently
    collide with every other empty one.
    """
    if not isinstance(raw, str):  # pragma: no cover - defensive; parser guarantees str
        raise TypeError(f"expected a string, got {type(raw).__name__}")

    # NFKC first: it maps full-width Latin letters (common in Chinese-authored
    # spreadsheets) onto ordinary ASCII, so a full-width "Ａ" does not survive as a
    # distinct character.
    text = unicodedata.normalize("NFKC", raw)
    text = _INVISIBLE.sub("", text)
    text = text.translate(_PUNCTUATION_TABLE)
    text = _WHITESPACE.sub(" ", text).strip()
    # Trailing separators are typing artefacts, not content.
    text = text.rstrip(".,;:").strip()
    # casefold, not lower: it handles the non-ASCII cases lower() misses.
    text = text.casefold()

    if not text:
        raise ValueError(f"institution name is empty after normalisation: {raw!r}")
    return text


def names_differ_only_by_presentation(left: str, right: str) -> bool:
    """Whether two names share a match key.

    Used by the importer to report a NAME_CHANGED difference only when the change is
    substantive, so a supplier's re-typed apostrophe does not appear as a rename.
    """
    return normalize_institution_name(left) == normalize_institution_name(right)


__all__ = ["names_differ_only_by_presentation", "normalize_institution_name"]
