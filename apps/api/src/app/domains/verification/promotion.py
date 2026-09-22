"""Promoting a verified mapping onto an acquisition source. The missing writer.

WHAT WAS MISSING, AND WHY IT MATTERED
=====================================
The trust chain is enforced end to end::

    official_domain -> source_mapping -> promoted_source_id -> source -> C27

and `source_mapping.promoted_source_id` was written by nothing outside tests and a
migration. Step 5C.5 reported it as the first blocker: every rule was in place and the
one act that crosses from "a reviewer trusts this page" to "this source may support a
published fact" had no implementation.

PROMOTION FOLLOWS TRUST; IT NEVER CREATES IT
============================================
Section 20. This module reads verification decisions and refuses when they are absent.
It cannot verify a domain, cannot verify a responsibility, and does not do either as a
side effect of promoting. If `promote` raises `NotYetTrusted`, the answer is for a
reviewer to make a decision, never for this code to make one for them.

That is also why promotion is explicit rather than automatic (section 21). A mapping
does not promote itself the moment it is verified: verification says *this page is what
it claims to be*, and promotion says *and we are now going to rely on it*. Keeping them
apart makes the second observable, testable, and separately revocable.

WHAT IT WRITES
==============
Two things, in one transaction:

1. `source_mapping.promoted_source_id`, which is what `source_eligibility_is_earned`
   looks for, and then `source.publication_eligibility`, which is what C27 reads;
2. the `source_field_binding` rows implied by the mapping's `source_category`, which is
   what the Step 5C.6 scope check reads. Without these the source is eligible and
   authorised for nothing, which is the correct failure mode but a confusing one to
   debug, so they are written by the same act that earns the eligibility.

TWO GUARDS ADDED IN STEP 5C.7L
==============================
**The manifest binding.** `promote` now requires a `ReviewedResponsibility` -- the object
`responsibility_binding.require_binding` returns, and the only way to obtain a genuine one
is to pass a matching manifest digest and have every link of the lineage agree. Promotion
is the act that makes a page publishable, so it must be as tightly bound to the reviewed
packet as the responsibility decision was. The object is checked here against the mapping
it claims to describe, because an argument nobody compares is an argument nobody checked.

(In-process callers can of course construct the dataclass by hand; tests do. That is a
test affordance, not an operator path -- the CLI and the console both reach promotion only
through `require_binding`.)

**Effective redirect authority.** Three of the six ANU mappings 301 to `study.anu.edu.au`,
and until now only the *requested* host's verification was consulted. The bytes that get
published come from wherever the fetch landed, so `redirect_authority` is asked whether
that host is trusted too, and promotion refuses when it is not.

The refusal is scoped to a *known* redirect into an untrusted or foreign host. A mapping
with no stored snapshot is not refused: there is no evidence of a redirect, and the
requested host's own verification -- checked immediately above -- is what covers that case.
Refusing on missing evidence would be a stricter rule than the trust chain asks for and
would strand every mapping whose evidence arrived by another route. The promotion preview
shows the fact instead. See `redirect_authority` for why a NULL `effective_url` is a
non-redirect rather than missing evidence.

LOCK ORDER
==========
Unchanged from `onboarding/verification.py`, and for the same reason::

    official_domain -> source_mapping -> source -> audit_chain_head

The audit append is last in every operation in this codebase because
`audit_chain_head` serialises every consequential write (C18) and its lock is held to
end of transaction. The order must be consistent across modules or two transactions form
a cycle.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, text

from app.core.logging import get_logger
from app.domains.verification import redirect_authority
from app.domains.verification.audit import append as append_audit
from app.domains.verification.policy import bindings_for
from app.domains.verification.responsibility_binding import ReviewedResponsibility

logger = get_logger(__name__)

#: What `source.publication_eligibility` may become, keyed by the mapping's derived
#: class. A mapping that derives `NOT_ELIGIBLE` promotes nothing.
PROMOTABLE = frozenset({"OFFICIAL_VERIFIED", "AUTHORIZED_EXTERNAL", "AUTHORIZED_RANKING"})

#: Redirect verdicts that stop a promotion. `NO_EVIDENCE` is absent on purpose -- see the
#: comment at the call site. These three all mean a host that actually served content is
#: not one this institution has verified.
_REDIRECT_REFUSALS = frozenset(
    {
        redirect_authority.AuthorityVerdict.REQUESTED_UNTRUSTED,
        redirect_authority.AuthorityVerdict.EFFECTIVE_UNTRUSTED,
        redirect_authority.AuthorityVerdict.INSTITUTION_MISMATCH,
    }
)


class PromotionRefusedError(RuntimeError):
    """The promotion cannot be performed as asked. The message says which check failed."""


class NotYetTrustedError(PromotionRefusedError):
    """A human decision this promotion depends on has not been made.

    Separate from the general refusal because the remedy is different: this one is not
    a bug to fix or an argument to correct, it is a person who has not yet looked.
    """


@dataclass(frozen=True, slots=True)
class Actor:
    """Who is promoting. A promotion with no actor is not a promotion."""

    id: uuid.UUID
    display: str


@dataclass(frozen=True, slots=True)
class PromotionResult:
    """What happened, including the case where nothing needed to."""

    mapping_id: uuid.UUID
    source_id: uuid.UUID
    eligibility: str
    responsibility: str
    bindings_written: int
    already_promoted: bool
    """True when the mapping already pointed at this source. See `promote`."""

    authority: redirect_authority.RedirectAuthority | None = None
    """Which hosts served this page, and whether both are trusted. Surfaced so the
    console can show the reviewer the redirect it is promoting across."""


def promote(
    connection: Connection,
    *,
    mapping_id: uuid.UUID,
    source_id: uuid.UUID,
    actor: Actor,
    reason: str,
    binding: ReviewedResponsibility,
    dry_run: bool = False,
) -> PromotionResult:
    """Point a verified mapping at a source, and earn that source its eligibility.

    Ten checks, in one transaction, in the order a reviewer would ask them. Each failure
    names the thing to go and do; none of them is repaired here.

    Idempotent (section 7). A mapping already promoted to this same source returns its
    current state with `already_promoted=True` and writes no audit row: repeating a
    request is not a second decision, and recording it as one would put a state
    transition in the history that never happened.
    """
    if not reason.strip():
        raise PromotionRefusedError("a promotion must record why it was made")

    # 0. The reviewed packet. Checked against the mapping it claims to describe, so a
    #    binding obtained for one mapping cannot be spent on another.
    if binding.mapping_id != mapping_id:
        raise PromotionRefusedError(
            f"the manifest binding describes mapping {binding.mapping_id} and this "
            f"promotion names {mapping_id}. A reviewed packet is not transferable."
        )

    mapping = connection.execute(
        text(
            """
            SELECT sm.id, sm.source_category::text AS responsibility, sm.url_sha256,
                   sm.verification_status::text AS status, sm.is_active,
                   sm.publication_eligibility, sm.promoted_source_id,
                   sm.official_domain_id, sm.target_institution_id,
                   od.verification_status::text AS domain_status,
                   od.is_active AS domain_active, od.host
              FROM source_mapping sm
              LEFT JOIN official_domain od ON od.id = sm.official_domain_id
             WHERE sm.id = :mapping
             FOR UPDATE OF sm
            """
        ),
        {"mapping": mapping_id},
    ).one_or_none()
    if mapping is None:
        raise PromotionRefusedError(f"no source_mapping {mapping_id}")

    if binding.claimed_responsibility != str(mapping.responsibility):
        raise PromotionRefusedError(
            f"the manifest binding reviews {binding.claimed_responsibility} and the "
            f"mapping asserts {mapping.responsibility}"
        )
    if binding.institution_id != mapping.target_institution_id:
        raise PromotionRefusedError(
            f"the manifest binding belongs to institution {binding.institution_id} and "
            f"the mapping to {mapping.target_institution_id}"
        )

    # 1-2. The host, and whether its verification still stands.
    if mapping.official_domain_id is None:
        raise NotYetTrustedError(
            f"mapping {mapping_id} names no official_domain: verify the host first"
        )
    if mapping.domain_status not in ("VERIFIED_OFFICIAL", "AUTHORIZED_EXTERNAL"):
        raise NotYetTrustedError(
            f"host {mapping.host} is {mapping.domain_status}, so nothing under it may be "
            "promoted"
        )
    if not mapping.domain_active:
        raise NotYetTrustedError(
            f"host {mapping.host} has been deactivated; its verification no longer stands"
        )

    # 2b. Where the bytes actually came from. The requested host's verification says
    #     nothing about a host it redirected to, and it is the redirect target's content
    #     that would become publishable.
    #
    #     NO_EVIDENCE is deliberately not a refusal here. It means no snapshot is stored,
    #     so there is no *known* redirect -- and the requested host's own verification,
    #     already checked above, is exactly what covers that case. Refusing would be a
    #     stricter policy than the trust chain asks for, and it would block every mapping
    #     whose evidence was acquired by some other route. The fact is surfaced on the
    #     promotion preview instead, so a reviewer confirms it with their eyes open.
    authority = redirect_authority.for_mapping(connection, mapping_id)
    if authority.verdict in _REDIRECT_REFUSALS:
        raise NotYetTrustedError(f"redirect authority: {authority.blocker}")

    # 4-5. The responsibility decision on this exact page.
    if mapping.status not in ("VERIFIED_OFFICIAL", "AUTHORIZED_EXTERNAL"):
        raise NotYetTrustedError(
            f"mapping {mapping_id} is {mapping.status}: its responsibility "
            f"{mapping.responsibility} has not been verified"
        )
    if not mapping.is_active:
        raise PromotionRefusedError(f"mapping {mapping_id} is inactive")

    # 6. The eligibility the database derives, which no role can write.
    if mapping.publication_eligibility not in PROMOTABLE:
        raise PromotionRefusedError(
            f"mapping {mapping_id} derives {mapping.publication_eligibility}; "
            "there is nothing to promote"
        )

    # 7-8. The target source, and whether it is still the right one to rely on.
    source = connection.execute(
        text(
            """
            SELECT id, url_hash, is_active, superseded_by_source_id,
                   publication_eligibility::text AS eligibility
              FROM source WHERE id = :source FOR UPDATE
            """
        ),
        {"source": source_id},
    ).one_or_none()
    if source is None:
        raise PromotionRefusedError(f"no source {source_id}")
    if source.superseded_by_source_id is not None:
        raise PromotionRefusedError(
            f"source {source_id} was superseded by {source.superseded_by_source_id}; "
            "promote the replacement instead"
        )
    if not source.is_active:
        raise PromotionRefusedError(f"source {source_id} is inactive")
    if source.url_hash != mapping.url_sha256:
        raise PromotionRefusedError(
            "the mapping and the source describe different URLs, so promoting would "
            "attach this page's authority to another page's evidence"
        )

    # 3. The mapping must not already belong to a different source.
    if mapping.promoted_source_id is not None and mapping.promoted_source_id != source_id:
        raise PromotionRefusedError(
            f"mapping {mapping_id} is already promoted to {mapping.promoted_source_id}"
        )

    # Section 7: already done is not a new decision.
    if (
        mapping.promoted_source_id == source_id
        and source.eligibility == mapping.publication_eligibility
    ):
        return PromotionResult(
            mapping_id=mapping_id,
            source_id=source_id,
            eligibility=str(source.eligibility),
            responsibility=str(mapping.responsibility),
            bindings_written=0,
            already_promoted=True,
            authority=authority,
        )

    pairs = bindings_for(str(mapping.responsibility))
    if not pairs:
        raise PromotionRefusedError(
            f"responsibility {mapping.responsibility} authorises no publishable field, "
            "so promoting it would earn an eligibility that permits nothing"
        )

    if dry_run:
        return PromotionResult(
            mapping_id=mapping_id,
            source_id=source_id,
            eligibility=str(mapping.publication_eligibility),
            responsibility=str(mapping.responsibility),
            bindings_written=len(pairs),
            already_promoted=False,
            authority=authority,
        )

    connection.execute(
        text("UPDATE source_mapping SET promoted_source_id = :source WHERE id = :mapping"),
        {"source": source_id, "mapping": mapping_id},
    )
    connection.execute(
        text(
            """
            UPDATE source
               SET publication_eligibility = CAST(:eligibility AS publication_eligibility),
                   eligibility_set_by = :actor,
                   eligibility_set_at = now(),
                   eligibility_reason = :reason
             WHERE id = :source
            """
        ),
        {
            "eligibility": mapping.publication_eligibility,
            "actor": actor.id,
            "reason": reason,
            "source": source_id,
        },
    )
    written = 0
    for entity_type, field_path in pairs:
        result = connection.execute(
            text(
                """
                INSERT INTO source_field_binding (source_id, entity_type, field_path,
                                                  responsibility)
                VALUES (:source, :entity, :field, 'PRIMARY')
                ON CONFLICT (source_id, entity_type, field_path) DO NOTHING
                """
            ),
            {"source": source_id, "entity": entity_type, "field": field_path},
        )
        written += result.rowcount or 0

    append_audit(
        connection,
        actor_id=actor.id,
        action="SOURCE_MAPPING_PROMOTED",
        object_type="source_mapping",
        object_id=mapping_id,
        reason=reason,
        after={
            "promoted_source_id": str(source_id),
            "responsibility": str(mapping.responsibility),
            "eligibility": str(mapping.publication_eligibility),
            "bindings": written,
        },
    )
    logger.info(
        "source_mapping_promoted",
        mapping_id=str(mapping_id),
        source_id=str(source_id),
        responsibility=str(mapping.responsibility),
        bindings=written,
    )
    return PromotionResult(
        mapping_id=mapping_id,
        source_id=source_id,
        eligibility=str(mapping.publication_eligibility),
        responsibility=str(mapping.responsibility),
        bindings_written=written,
        already_promoted=False,
        authority=authority,
    )


__all__ = [
    "PROMOTABLE",
    "Actor",
    "NotYetTrustedError",
    "PromotionRefusedError",
    "PromotionResult",
    "promote",
]
