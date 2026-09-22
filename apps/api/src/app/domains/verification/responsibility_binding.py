"""A responsibility decision names the manifest row it was made from, or it is refused.

WHAT WAS WRONG
==============
``source_review.py responsibility`` took a mapping UUID and a decision. It validated the
frozen manifest's digest -- but only when ``--expect-sha256`` happened to be passed, since
the flag defaulted to ``None`` and ``_require_manifest`` returned immediately on ``None``.
Worse, even a *correct* digest proved only that the package on disk was the approved one.
Nothing tied the UUID being decided to a row inside that package.

The two failures that made possible:

1. A mistyped UUID applies a reviewer's judgement to a different mapping. While only one
   institution is registered the blast radius is small; the moment a second exists, one
   institution's approval spends itself on another institution's source.
2. A reviewer who genuinely approved package X can decide a mapping that package X never
   described, and the audit trail will show a decision backed by a manifest that never
   mentioned it.

THE LINEAGE THAT MUST AGREE
===========================
A responsibility decision is only meaningful if every link holds::

    source_mapping
      -> pilot_collected_source   (the workbook row it was registered from)
      -> frozen manifest row      (the packet the human actually read)
      -> institution              (whose property this is)
      -> source_ref               (which workbook row)
      -> claimed responsibility   (what is being asserted)
      -> requested URL            (which page)

Five things are compared, not one. A matching digest with a mismatched institution,
source_ref, responsibility or URL is a refusal, because each of those is a different way
for a decision to land on something the reviewer did not look at.

WHY ``source_ref`` IS THE JOIN KEY
==================================
The manifest is keyed by ``source_ref``, which is what a reviewer reads and what the
workbook row carries. URL alone will not do: S0236 and S0242 are the *same physical page*
submitted under two different responsibilities, so a URL match is ambiguous by
construction. ``source_ref`` is unique within the manifest and is checked to be so here.

ONE HELPER, TWO CALLERS
=======================
The CLI and the reviewer console both call this. The policy lives here and is not
restated in TypeScript: a rule implemented twice is a rule that will disagree with
itself, and the half a reviewer sees is not the half that guards the write.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from app.domains.verification.domain_binding import (
    MANIFEST_DIR,
    InstitutionBindingError,
    ManifestChangedError,
    manifest_digest,
    resolve_institution,
)

#: The responsibility manifest's basename, without extension.
RESPONSIBILITY_MANIFEST = "responsibility_decisions_proposed"


class ResponsibilityBindingError(InstitutionBindingError):
    """The mapping, the manifest row and the decision do not describe the same thing.

    Subclasses ``InstitutionBindingError`` so a caller that already refuses on binding
    failures keeps refusing without having to learn a new exception type.
    """


@dataclass(frozen=True, slots=True)
class ReviewedResponsibility:
    """One responsibility question exactly as the approved manifest poses it."""

    mapping_id: uuid.UUID
    pilot_source_id: uuid.UUID
    source_ref: str
    institution_label: str
    institution_id: uuid.UUID
    claimed_responsibility: str
    url: str
    host: str
    access_class: str
    page_evidence_available: bool
    is_duplicate_row: bool
    duplicate_of: str | None
    declared_degree_scope: str | None
    manifest_sha256: str

    @property
    def same_page_as(self) -> str | None:
        """The ``source_ref`` this row shares a physical page with, if any.

        S0236/S0242 is the live instance: one fetched page, two responsibilities. The
        console must say so, because a reviewer shown the same body twice without that
        sentence will reasonably think something is duplicated by mistake.
        """
        return self.duplicate_of


def load_responsibility_manifest(
    *, expect_sha256: str, directory: Path = MANIFEST_DIR
) -> tuple[list[dict[str, Any]], str]:
    """Read the frozen responsibility manifest and prove it is the approved one.

    ``expect_sha256`` is required, not optional, and empty is not accepted. The previous
    signature allowed ``None`` and skipped the check -- an optional guard is a guard that
    is absent exactly when somebody is in a hurry.
    """
    if not expect_sha256 or not expect_sha256.strip():
        raise ManifestChangedError(
            "REVIEW_MANIFEST_CHANGED: no approved manifest digest was supplied. A "
            "responsibility decision must name the exact package the reviewer read."
        )
    path = directory / f"{RESPONSIBILITY_MANIFEST}.json"
    if not path.exists():
        raise ManifestChangedError(
            f"REVIEW_MANIFEST_CHANGED: {path} does not exist. It is NOT regenerated "
            "automatically -- a manifest rebuilt by the applying code is a manifest "
            "nobody reviewed."
        )
    envelope = json.loads(path.read_text(encoding="utf-8"))
    rows = list(envelope.get("rows") or [])
    digest = manifest_digest(rows)
    if digest != expect_sha256.strip():
        raise ManifestChangedError(
            "REVIEW_MANIFEST_CHANGED\n"
            f"  approved : {expect_sha256}\n  on disk  : {digest}\n"
            "The package under review is not the one being applied."
        )
    return rows, digest


def manifest_row(rows: list[dict[str, Any]], *, source_ref: str) -> dict[str, Any]:
    """The one manifest row for this ``source_ref``, or a refusal naming the ambiguity."""
    target = source_ref.strip().upper()
    found = [r for r in rows if str(r.get("source_ref", "")).strip().upper() == target]
    if not found:
        raise ResponsibilityBindingError(
            f"{source_ref} does not appear in the approved responsibility manifest. "
            "Only rows the reviewer was actually shown may be decided from it."
        )
    if len(found) > 1:
        raise ResponsibilityBindingError(
            f"{source_ref} appears {len(found)} times in the approved manifest; it must "
            "appear once, or 'the reviewed row' is ambiguous."
        )
    return found[0]


def require_binding(
    connection: Connection,
    *,
    mapping_id: uuid.UUID,
    expect_sha256: str,
    directory: Path = MANIFEST_DIR,
) -> ReviewedResponsibility:
    """Bind one ``source_mapping`` to the approved manifest row that describes it.

    Refuses unless the whole lineage agrees. This is the check that was missing: the
    digest says the package is approved, and this says *this mapping is in it*.
    """
    rows, digest = load_responsibility_manifest(expect_sha256=expect_sha256, directory=directory)

    lineage = connection.execute(
        text(
            """
            SELECT sm.id                         AS mapping_id,
                   sm.source_category::text      AS responsibility,
                   sm.url                        AS mapping_url,
                   sm.host                       AS mapping_host,
                   sm.target_institution_id      AS mapping_institution,
                   pcs.id                        AS pilot_source_id,
                   pcs.source_ref                AS source_ref,
                   pcs.official_url              AS pilot_url,
                   pcs.target_institution_id     AS pilot_institution,
                   pcs.source_type::text         AS pilot_responsibility,
                   pcs.degree_scope::text        AS degree_scope
              FROM source_mapping sm
              LEFT JOIN pilot_collected_source pcs
                     ON pcs.promoted_source_mapping_id = sm.id
             WHERE sm.id = :mapping
            """
        ),
        {"mapping": mapping_id},
    ).one_or_none()
    if lineage is None:
        raise ResponsibilityBindingError(f"no source_mapping {mapping_id}")
    if lineage.pilot_source_id is None:
        raise ResponsibilityBindingError(
            f"mapping {mapping_id} was not registered from a pilot workbook row, so "
            "there is no reviewed packet to bind the decision to."
        )

    row = manifest_row(rows, source_ref=str(lineage.source_ref))

    # 1. Institution. The manifest names a label; the label resolves to exactly one id.
    label = str(row.get("institution") or "").strip()
    if not label:
        raise ResponsibilityBindingError(
            f"the approved manifest row for {lineage.source_ref} names no institution, "
            "so the decision cannot be attributed to one."
        )
    institution_id = resolve_institution(connection, label)
    if uuid.UUID(str(lineage.mapping_institution)) != institution_id:
        raise ResponsibilityBindingError(
            f"institution mismatch for {lineage.source_ref}: the approved manifest "
            f"reviews it under {label!r} ({institution_id}), and the mapping belongs to "
            f"{lineage.mapping_institution}. A decision cannot be moved between "
            "institutions."
        )
    if uuid.UUID(str(lineage.pilot_institution)) != institution_id:
        raise ResponsibilityBindingError(
            f"the workbook row {lineage.source_ref} belongs to "
            f"{lineage.pilot_institution}, not to {label!r} ({institution_id})."
        )

    # 2. Responsibility. What is being asserted must be what was asked about.
    claimed = str(row.get("claimed_responsibility") or "").strip()
    if claimed != str(lineage.responsibility):
        raise ResponsibilityBindingError(
            f"responsibility mismatch for {lineage.source_ref}: the manifest asks about "
            f"{claimed!r} and the mapping asserts {lineage.responsibility!r}."
        )
    if claimed != str(lineage.pilot_responsibility):
        raise ResponsibilityBindingError(
            f"responsibility mismatch for {lineage.source_ref}: the manifest asks about "
            f"{claimed!r} and the workbook row declares {lineage.pilot_responsibility!r}."
        )

    # 3. The page. Compared against the requested/original URL, which is what the
    #    manifest shows and what the mapping stores. The effective URL after redirects
    #    is a separate question, answered by `redirect_authority`.
    url = str(row.get("url") or "").strip()
    if url != str(lineage.mapping_url):
        raise ResponsibilityBindingError(
            f"URL mismatch for {lineage.source_ref}: the manifest reviews {url!r} and "
            f"the mapping records {lineage.mapping_url!r}."
        )
    if url != str(lineage.pilot_url):
        raise ResponsibilityBindingError(
            f"URL mismatch for {lineage.source_ref}: the manifest reviews {url!r} and "
            f"the workbook row records {lineage.pilot_url!r}."
        )

    return ReviewedResponsibility(
        mapping_id=uuid.UUID(str(lineage.mapping_id)),
        pilot_source_id=uuid.UUID(str(lineage.pilot_source_id)),
        source_ref=str(lineage.source_ref),
        institution_label=label,
        institution_id=institution_id,
        claimed_responsibility=claimed,
        url=url,
        host=str(row.get("host") or lineage.mapping_host),
        access_class=str(row.get("access_class") or "UNKNOWN"),
        page_evidence_available=bool(row.get("page_evidence_available")),
        is_duplicate_row=bool(row.get("is_duplicate_row")),
        duplicate_of=(str(row["duplicate_of"]) if row.get("duplicate_of") else None),
        declared_degree_scope=(
            str(row["declared_degree_scope"]) if row.get("declared_degree_scope") else None
        ),
        manifest_sha256=digest,
    )


__all__ = [
    "MANIFEST_DIR",
    "RESPONSIBILITY_MANIFEST",
    "ManifestChangedError",
    "ResponsibilityBindingError",
    "ReviewedResponsibility",
    "load_responsibility_manifest",
    "manifest_row",
    "require_binding",
]
