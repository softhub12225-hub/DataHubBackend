"""Deciding what bytes mean, deterministically (Step 5C.1 section 8).

WHY NOT A DETECTION LIBRARY
===========================
`charset-normalizer` and `chardet` guess, and they guess differently between versions.
An extraction whose output depends on a dependency's heuristics is not reproducible, and
reproducibility is the property this whole step is built on -- the artifact hash has to
be a function of the bytes and the extractor version, nothing else.

So this is a fixed ladder, in the order the standards say to trust:

1. the **HTTP** `Content-Type` charset, because the server stated it about these bytes;
2. the **document's own** declaration -- BOM, then `<meta charset>`, then the older
   `<meta http-equiv="Content-Type">`;
3. UTF-8, which is right for the overwhelming majority of the web;
4. `cp1252`, which decodes every byte and is a superset of Latin-1 in the places that
   matter for European text.

Step 4 always succeeds, so there is no "undecodable" outcome -- but arriving there is
recorded as a fallback, and the count of replacement characters is recorded too, so a
page that decoded badly is visible rather than silently mangled.

THE REAL FLEET
==============
The pilot's 175 stored snapshots declare six different content-type spellings including
one `ISO-8859-15`, and 16 declare no charset at all. Assuming UTF-8 would have corrupted
the first and guessed at the rest.

**Raw bytes are never rewritten.** This module reads them and returns text; the blob in
the object store is untouched, and any decoding decision can be revisited later by
re-running a new extractor version over the same bytes.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

#: Last resort. `cp1252` maps every one of the 256 byte values, so decoding cannot fail;
#: Latin-1 would too, but cp1252 additionally gets the curly quotes and dashes that
#: Windows-authored pages actually contain.
FINAL_FALLBACK = "cp1252"

#: Tried before the fallback. UTF-8 first because it is both the modern default and
#: self-validating: invalid UTF-8 is detectable, which is what makes this ladder safe.
PREFERRED = "utf-8"

#: How far into the document to look for a declaration. The HTML spec's own prescan
#: limit is 1024 bytes; real pages occasionally push the meta tag past it behind
#: comments and conditional blocks, so this is deliberately more generous.
DECLARATION_WINDOW = 4096

_META_CHARSET = re.compile(rb"""<meta[^>]+?charset\s*=\s*["']?\s*([a-zA-Z0-9_\-:.]+)""", re.I)
_META_CONTENT_TYPE = re.compile(
    rb"""<meta[^>]+?http-equiv\s*=\s*["']?content-type["']?[^>]*?"""
    rb"""content\s*=\s*["'][^"']*?charset\s*=\s*([a-zA-Z0-9_\-:.]+)""",
    re.I,
)

#: Byte-order marks, longest first so UTF-32 is not mistaken for UTF-16.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


@dataclass(frozen=True, slots=True)
class DecodedText:
    """The text, and an honest account of how it was obtained."""

    text: str
    #: What the HTTP header said, verbatim and lowercased, or None.
    declared_http: str | None
    #: What the document said about itself, or None.
    declared_document: str | None
    #: The codec actually used.
    used: str
    #: True when neither declaration was usable and the ladder fell through.
    fallback_used: bool
    #: U+FFFD count. Non-zero means the chosen codec was wrong somewhere, which a
    #: reader of the extraction needs to know before trusting the wording.
    replacement_characters: int
    #: Every codec tried and rejected, in order, with why.
    attempts: list[str] = field(default_factory=list)

    @property
    def lossy(self) -> bool:
        return self.replacement_characters > 0

    def as_metadata(self) -> dict[str, object]:
        return {
            "declared_http": self.declared_http,
            "declared_document": self.declared_document,
            "used": self.used,
            "fallback_used": self.fallback_used,
            "replacement_characters": self.replacement_characters,
            "lossy": self.lossy,
            "attempts": list(self.attempts),
        }


def charset_from_content_type(content_type: str | None) -> str | None:
    """The `charset=` parameter of a `Content-Type`, or None."""
    if not content_type:
        return None
    for part in content_type.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            cleaned = value.strip().strip("\"'").lower()
            return cleaned or None
    return None


def charset_from_document(payload: bytes) -> str | None:
    """What the document declares about itself: BOM, then meta, then http-equiv.

    A BOM outranks a `<meta>` tag because it is unambiguous and because a document
    whose BOM and meta disagree is a document whose meta tag is wrong.
    """
    for bom, name in _BOMS:
        if payload.startswith(bom):
            return name
    window = payload[:DECLARATION_WINDOW]
    for pattern in (_META_CHARSET, _META_CONTENT_TYPE):
        found = pattern.search(window)
        if found:
            return found.group(1).decode("ascii", "ignore").strip().lower() or None
    return None


def _normalise(name: str | None) -> str | None:
    """Resolve a declared name to a codec Python knows, or None.

    Returning None rather than raising is deliberate: a page declaring
    `charset=unicode` or `charset=none` is common enough, and the answer is to move
    down the ladder rather than to fail the extraction.
    """
    if not name:
        return None
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def decode_document(payload: bytes, *, content_type: str | None = None) -> DecodedText:
    """Decode stored bytes to text by the fixed ladder. Never raises.

    The bytes are not modified and not re-encoded; this returns a *view* of them as
    text, and the blob remains the authoritative evidence.
    """
    declared_http = charset_from_content_type(content_type)
    declared_document = charset_from_document(payload)
    attempts: list[str] = []

    for label, declared in (("http", declared_http), ("document", declared_document)):
        codec = _normalise(declared)
        if codec is None:
            if declared:
                attempts.append(f"{label}:{declared} (unknown codec)")
            continue
        try:
            text = payload.decode(codec, errors="strict")
        except (UnicodeDecodeError, LookupError) as exc:
            attempts.append(f"{label}:{codec} ({type(exc).__name__})")
            continue
        return DecodedText(
            text=text,
            declared_http=declared_http,
            declared_document=declared_document,
            used=codec,
            fallback_used=False,
            replacement_characters=0,
            attempts=attempts,
        )

    # Nothing usable was declared, or what was declared did not decode these bytes.
    try:
        text = payload.decode(PREFERRED, errors="strict")
    except UnicodeDecodeError as exc:
        attempts.append(f"fallback:{PREFERRED} ({type(exc).__name__})")
    else:
        return DecodedText(
            text=text,
            declared_http=declared_http,
            declared_document=declared_document,
            used=codecs.lookup(PREFERRED).name,
            # A fallback even though it succeeded: nothing *told* us it was UTF-8.
            fallback_used=True,
            replacement_characters=0,
            attempts=attempts,
        )

    text = payload.decode(FINAL_FALLBACK, errors="replace")
    return DecodedText(
        text=text,
        declared_http=declared_http,
        declared_document=declared_document,
        used=codecs.lookup(FINAL_FALLBACK).name,
        fallback_used=True,
        replacement_characters=text.count("�"),
        attempts=attempts,
    )


__all__ = [
    "DECLARATION_WINDOW",
    "FINAL_FALLBACK",
    "PREFERRED",
    "DecodedText",
    "charset_from_content_type",
    "charset_from_document",
    "decode_document",
]
