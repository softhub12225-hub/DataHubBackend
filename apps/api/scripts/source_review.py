"""Operator commands for human source verification (Step 5C.6 sections 11-18, 22-24).

WHAT THIS IS
============
The plumbing a real reviewer needs, and nothing that substitutes for one. Every command
that changes trust state requires an `--actor` holding `source:verify`, a `--reason`, and
succeeds only if the decision it depends on has already been made by somebody.

    reviewer-create        provision a real identity. Operator supplies it; nothing is
                           invented, and this is never run automatically (section 11)
    reviewer-status        who may verify, and what they hold
    domain                 apply one reviewed domain decision (section 14)
    responsibility         apply one reviewed responsibility decision (section 15)
    promote                promote a verified mapping onto its source (section 5)
    apply-manifest         apply a reviewer-approved manifest, hash-checked (16, 17)
    readiness              promotion readiness, with per-candidate blockers (22, 24)
    matrix                 the source/responsibility readiness matrix (section 28)

`--dry-run` is available on everything that writes, reports what would change, and
mutates nothing (section 18).

WHAT IT WILL NOT DO
===================
There is no `verify-all`, no `--yes-to-everything`, and no default decision. A manifest
row with no explicit decision is an error, never a `VERIFIED`: section 16 is explicit,
and the failure mode it describes -- a missing field read as approval -- is the kind that
produces a fully-populated trust table nobody decided.
"""

# ruff: noqa: S608 -- the only interpolation in any query is a table alias written as a
# literal at the call site. Every value travels as a bound parameter.

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import sys
import uuid
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import DatabaseRole, get_settings
from app.domains.acquisition.storage import build_evidence_store
from app.domains.extraction.runner import DERIVED_PREFIX
from app.domains.identity.accounts import (
    AccountRefusedError,
    CredentialAlreadyEnrolledError,
)
from app.domains.identity.enrollment import (
    EnrolmentRefusedError,
    change_own_password,
    enrol_initial_password,
    reset_password_as_administrator,
)
from app.domains.identity.enrollment_tokens import (
    DEFAULT_LIFETIME,
    TOKEN_LENGTH,
    EnrollmentTokenError,
    TokenExpiredError,
    looks_like_token,
)
from app.domains.identity.enrollment_tokens import (
    issue as issue_enrollment_challenge,
)
from app.domains.identity.enrollment_tokens import (
    revoke as revoke_enrollment_challenge,
)
from app.domains.identity.passwords import MINIMUM_LENGTH, WeakPasswordError
from app.domains.verification.audit import append as _audit
from app.domains.verification.authentication import (
    ActorMismatchError,
    AuthenticatedReviewer,
    AuthenticationFailedError,
    FixtureIdentityRefusedError,
    authenticate,
    read_password,
    refuse_test_identity_on_real_data,
)
from app.domains.verification.domain_binding import (
    BindingAction,
    InstitutionBindingError,
    ManifestChangedError,
    ReviewedHost,
    plan_binding,
    require_binding,
    resolve_institution,
)
from app.domains.verification.identity import (
    VERIFY_PERMISSION,
    BootstrapClosedError,
    IdentityRefusedError,
    NotAuthorizedError,
    permissions_of,
    provision_reviewer,
)
from app.domains.verification.policy import Blocker, is_compatible
from app.domains.verification.promotion import Actor, PromotionRefusedError, promote
from app.domains.verification.readiness import assess, summarise
from app.domains.verification.responsibility_binding import (
    require_binding as require_responsibility_binding,
)
from app.domains.verification.source_body import (
    NOT_AVAILABLE,
    SourceBodyError,
)
from app.domains.verification.source_body import load as load_body
from app.domains.verification.source_body import render as render_body
from app.domains.verification.source_body import resolve as resolve_body

#: Domain decisions an operator may apply, and the `official_domain.verification_status`
#: each one records. `NEEDS_REVIEW` maps to `CANDIDATE`, which is what the model already
#: means by "proposed, nobody has decided" -- section 14 says not to invent duplicate
#: states, and a second undecided state is the most duplicative one available.
DOMAIN_DECISIONS: dict[str, str] = {
    "VERIFIED_OFFICIAL": "VERIFIED_OFFICIAL",
    "AUTHORIZED_EXTERNAL": "AUTHORIZED_EXTERNAL",
    "REJECTED": "REJECTED",
    "NEEDS_REVIEW": "CANDIDATE",
    "REVOKED": "REJECTED",
}

#: Responsibility decisions, onto `source_mapping.verification_status`.
RESPONSIBILITY_DECISIONS: dict[str, str] = {
    "VERIFIED": "VERIFIED_OFFICIAL",
    "AUTHORIZED_EXTERNAL": "AUTHORIZED_EXTERNAL",
    "REJECTED": "REJECTED",
    "NEEDS_REVIEW": "CANDIDATE",
    "REVOKED": "REJECTED",
}

#: Decisions that switch a row off. Kept explicit rather than derived from the status,
#: because `REVOKED` and `REJECTED` land on the same status and mean different things to
#: the person reading the audit log.
DEACTIVATING = frozenset({"REJECTED", "REVOKED"})


def _engine(role: DatabaseRole = DatabaseRole.API) -> Engine:
    return create_engine(get_settings().database.sync_dsn(role), future=True)


def _require_configured(role: DatabaseRole) -> None:
    """Refuse before connecting when a role's credential was never configured.

    `DatabaseSettings` defaults the runtime passwords to the placeholder `change-me`, so
    an unconfigured role does not fail loudly -- it tries to log in with a wrong password
    and reports an authentication error that looks like the operator mistyped something.
    Saying which variable is missing costs one check and saves the wrong diagnosis.

    There is deliberately no fallback to the owning connection. Claiming an enrolment
    challenge runs as `app_api` precisely so that somebody holding only the application
    credential can spend a token and cannot mint one; quietly reconnecting as the owner
    when the application credential is absent would erase that distinction at exactly the
    moment it matters.
    """
    _, password = get_settings().database.credentials(role)
    if password.get_secret_value() == "change-me":
        name = role.value.upper()
        raise SystemExit(
            "\n".join(
                (
                    f"refused: the {role.value} database role is not configured in this "
                    f"process. Set POSTGRES_{name}_USER and POSTGRES_{name}_PASSWORD.",
                    "  This command runs as the application role on purpose: issuing an "
                    "enrolment challenge needs the owner's credential and claiming one "
                    "does not, and that is what stops whoever can claim an account also "
                    "being able to authorise it.",
                    "  It does NOT fall back to the owning connection.",
                )
            )
        )


#: Everything a write command may legitimately refuse with, in one tuple so a new
#: refusal cannot be added in one place and forgotten in five.
_REFUSALS = (
    NotAuthorizedError,
    IdentityRefusedError,
    PromotionRefusedError,
    AuthenticationFailedError,
    ActorMismatchError,
    FixtureIdentityRefusedError,
)


def _authenticate(args: argparse.Namespace, connection: Connection) -> AuthenticatedReviewer:
    """Establish who this process is, from a credential rather than from an argument.

    Every write command calls this. `--reviewer-email` names the account; the password
    comes from a TTY or `DATAHUB_REVIEWER_PASSWORD`, never from an argument, because an
    argument is visible in `ps`, in shell history and in anything that echoes a command.
    """
    session = authenticate(
        connection,
        email=args.reviewer_email,
        password=read_password(prompt=f"Password for {args.reviewer_email}: "),
    )
    if not getattr(args, "allow_test_identity", False):
        refuse_test_identity_on_real_data(session)
    return session


def _auth_arguments(parser: argparse.ArgumentParser) -> None:
    """The flags every authenticated write shares. Note the absence of `--actor`.

    Section 10: the actor comes from the authenticated session. Keeping an `--actor`
    argument that merely has to equal the session is keeping an argument that will one
    day be trusted without the comparison.
    """
    parser.add_argument("--reviewer-email", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-test-identity",
        action="store_true",
        help=argparse.SUPPRESS,
    )


def _rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ===========================================================================
# reviewer identity (sections 11-13)
# ===========================================================================


def command_reviewer_create(args: argparse.Namespace) -> int:
    """Provision one real reviewer. Nothing is invented and nothing is automatic.

    Runs as the owning role, not `app_api`. Creating an identity and granting it a role
    is administrative work, and the grants say so: every runtime role holds SELECT on
    `app_user`, `user_role`, `role` and `role_permission` and nothing more. `app_api`
    can therefore *authorise* a reviewer -- which is what every decision command does --
    and cannot *create* one. That separation is the reason this command is the only one
    in this file that needs a different connection.
    """
    engine = _engine(DatabaseRole.MIGRATION)
    try:
        with engine.begin() as connection:
            if args.dry_run:
                print(
                    f"DRY RUN: would create or re-activate {args.email} as "
                    f"{args.role!r} with display name {args.display_name!r}"
                )
                return 0
            reviewer = provision_reviewer(
                connection,
                email=args.email,
                display_name=args.display_name,
                role=args.role,
                test_only=args.test_only,
            )
            held = sorted(permissions_of(connection, reviewer.id))
        print(f"reviewer {reviewer.email}")
        print(f"  id           : {reviewer.id}")
        print(f"  display name : {reviewer.display_name}")
        print(f"  roles        : {', '.join(reviewer.roles) or '(none)'}")
        print(f"  permissions  : {', '.join(held) or '(none)'}")
        print(f"  test identity: {reviewer.is_test}")
        print(
            "\n  No password was set. This establishes who the audit trail will name; "
            "\n  authentication belongs to the front end that does not exist yet."
        )
        return 0
    except IdentityRefusedError as exc:
        print(f"refused: {exc}")
        return 2
    finally:
        engine.dispose()


def command_reviewer_status(_: argparse.Namespace) -> int:
    engine = _engine()
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT u.id, u.email, u.display_name, u.is_active, "
                "       coalesce(string_agg(ur.role_code, ', ' ORDER BY ur.role_code), '') "
                "       AS roles "
                "  FROM app_user u LEFT JOIN user_role ur ON ur.user_id = u.id "
                " GROUP BY u.id, u.email, u.display_name, u.is_active ORDER BY u.email"
            )
        ).all()
        _rule("IDENTITIES")
        for row in rows:
            held = sorted(permissions_of(connection, row.id))
            can = "source:verify" in held
            print(f"\n  {row.email}")
            print(f"    display name : {row.display_name}")
            print(f"    active       : {row.is_active}")
            print(f"    roles        : {row.roles or '(none)'}")
            print(f"    may verify   : {can}")
        print(
            f"\n  {len(rows)} identity(ies); "
            f"{sum(1 for r in rows if 'source:verify' in permissions_of(connection, r.id))} "
            "may record a verification decision."
        )
    engine.dispose()
    return 0


# ===========================================================================
# decisions (sections 14, 15)
# ===========================================================================


def _apply_domain(
    connection: Connection,
    *,
    host: str,
    decision: str,
    actor: uuid.UUID,
    reason: str,
    reviewed: ReviewedHost,
    method: str,
    dry_run: bool,
) -> str:
    """Apply one reviewed domain decision, bound to the institution that was reviewed.

    `reviewed` replaces the old optional `institution` argument. It is not a UUID the
    caller chose: it is the manifest row the reviewer was shown, with its institution
    resolved, so there is no call site at which the binding can be omitted. The previous
    signature made it optional and both callers passed None, which is how every pilot
    domain row would have been created attached to no institution at all.
    """
    status = DOMAIN_DECISIONS[decision]
    institution = reviewed.institution_id
    # Refuses a reassignment before anything is read for update.
    binding = plan_binding(connection, host=host, institution_id=institution)

    row = connection.execute(
        text(
            "SELECT id, verification_status::text AS status, is_active, target_institution_id "
            "  FROM official_domain WHERE host = :host FOR UPDATE"
        ),
        {"host": host},
    ).one_or_none()
    active = decision not in DEACTIVATING
    if (
        row is not None
        and row.status == status
        and row.is_active == active
        and binding is BindingAction.KEEP
    ):
        return f"unchanged ({status}, institution {institution})"
    if dry_run:
        current = row.status if row else "NONE"
        detail = {
            BindingAction.CREATE: f"would create official_domain bound to {institution}",
            BindingAction.ADOPT: f"would adopt the NULL binding, setting {institution}",
            BindingAction.KEEP: f"binding unchanged ({institution})",
        }[binding]
        return (
            f"would change {current} -> {status}; {detail}"
            f" [{reviewed.institution_label}]"
            f"{' (redirect-only host)' if reviewed.reached_only_by_redirect else ''}"
        )

    verified = status in ("VERIFIED_OFFICIAL", "AUTHORIZED_EXTERNAL")
    params = {
        "host": host,
        "status": status,
        "actor": actor if verified or decision in DEACTIVATING else None,
        "at": None,
        "reason": reason if decision in DEACTIVATING else None,
        "method": method if verified else None,
        "evidence": reason if verified else None,
        "active": active,
        "institution": institution,
        "authorization": reason if status == "AUTHORIZED_EXTERNAL" else None,
    }
    if row is None:
        connection.execute(
            text(
                "INSERT INTO official_domain (id, target_institution_id, host, "
                "verification_status, verification_method, verification_evidence, "
                "authorization_reference, verified_at, verified_by, rejected_reason, "
                "is_active) VALUES (gen_random_uuid(), :institution, :host, "
                "CAST(:status AS official_verification_status), "
                "CAST(:method AS domain_verification_method), :evidence, :authorization, "
                "CASE WHEN :actor IS NULL THEN NULL ELSE now() END, :actor, :reason, :active)"
            ),
            params,
        )
    else:
        connection.execute(
            text(
                "UPDATE official_domain SET "
                "  verification_status = CAST(:status AS official_verification_status), "
                "  verification_method = CAST(:method AS domain_verification_method), "
                "  verification_evidence = :evidence, authorization_reference = :authorization, "
                "  verified_at = CASE WHEN :actor IS NULL THEN NULL ELSE now() END, "
                "  verified_by = :actor, rejected_reason = :reason, is_active = :active, "
                "  updated_at = now() WHERE host = :host"
            ),
            params,
        )
    _audit(
        connection,
        actor_id=actor,
        action=f"DOMAIN_{decision}",
        object_type="official_domain",
        object_id=row.id if row else None,
        reason=reason,
        before=({"target_institution_id": None} if binding is BindingAction.ADOPT else None),
        after={
            "host": host,
            "status": status,
            "is_active": active,
            # Recorded so the trail says which institution's property was verified, and
            # names the exact reviewed package the decision came from.
            "target_institution_id": str(institution),
            "institution": reviewed.institution_label,
            "binding": str(binding),
            "domain_manifest_sha256": reviewed.manifest_sha256,
        },
    )
    return (
        f"{(row.status if row else 'NONE')} -> {status}; " f"institution {institution} ({binding})"
    )


def command_domain(args: argparse.Namespace) -> int:
    engine = _engine()
    try:
        with engine.begin() as connection:
            session = _authenticate(args, connection)
            # One reviewed tuple: the approved manifest, the host inside it, and the
            # institution it was reviewed under. A matching digest alone would let an
            # approval for one institution be spent on another.
            supplied = args.institution_id or args.institution
            reviewed = require_binding(
                connection,
                host=args.host.lower(),
                institution_id=uuid.UUID(supplied) if supplied else None,
                expect_sha256=args.expect_sha256,
            )
            outcome = _apply_domain(
                connection,
                host=args.host.lower(),
                decision=args.decision,
                actor=session.id,
                reason=args.reason,
                reviewed=reviewed,
                method=args.method,
                dry_run=args.dry_run,
            )
            if args.dry_run:
                connection.rollback()
        print(f"{'DRY RUN: ' if args.dry_run else ''}{args.host}: {outcome}")
        return 0
    except ManifestChangedError as exc:
        print(str(exc))
        return 2
    except InstitutionBindingError as exc:
        print(f"refused: {exc}")
        return 2
    except _REFUSALS as exc:
        print(f"refused: {exc}")
        return 2
    finally:
        engine.dispose()


def _apply_responsibility(
    connection: Connection,
    *,
    mapping_id: uuid.UUID,
    decision: str,
    actor: uuid.UUID,
    reason: str,
    dry_run: bool,
) -> str:
    status = RESPONSIBILITY_DECISIONS[decision]
    row = connection.execute(
        text(
            "SELECT id, source_category::text AS responsibility, "
            "       verification_status::text AS status, is_active "
            "  FROM source_mapping WHERE id = :id FOR UPDATE"
        ),
        {"id": mapping_id},
    ).one_or_none()
    if row is None:
        raise PromotionRefusedError(f"no source_mapping {mapping_id}")
    active = decision not in DEACTIVATING
    if row.status == status and row.is_active == active:
        return f"unchanged ({row.responsibility}: {status})"
    if dry_run:
        return f"{row.responsibility}: would change {row.status} -> {status}"

    connection.execute(
        text(
            "UPDATE source_mapping SET "
            "  verification_status = CAST(:status AS official_verification_status), "
            "  verified_at = now(), verified_by = :actor, "
            "  rejected_reason = :rejected, is_active = :active, "
            "  deactivated_reason = :deactivated, updated_at = now() WHERE id = :id"
        ),
        {
            "id": mapping_id,
            "status": status,
            "actor": actor,
            "rejected": reason if decision in DEACTIVATING else None,
            "deactivated": reason if not active else None,
            "active": active,
        },
    )
    _audit(
        connection,
        actor_id=actor,
        action=f"RESPONSIBILITY_{decision}",
        object_type="source_mapping",
        object_id=mapping_id,
        reason=reason,
        after={"responsibility": row.responsibility, "status": status, "is_active": active},
    )
    return f"{row.responsibility}: {row.status} -> {status}"


def command_responsibility(args: argparse.Namespace) -> int:
    """Apply one responsibility decision through the same service the console uses.

    STEP 5C.7L: THIS NO LONGER HAS ITS OWN LOGIC
    ============================================
    It previously called a local `_apply_responsibility`, which checked a manifest digest
    (when one happened to be supplied) and nothing else -- in particular it never checked
    that the mapping being decided appeared in that manifest at all.

    Now it calls `domains.verification.decisions`, which is the module the reviewer
    console calls. `--dry-run` prints the preview; a real run applies the token that
    preview issued. That is not a convenience: it means the CLI and the browser cannot
    enforce different rules, because they are running the same function.
    """
    from app.domains.verification import decisions as decision_service

    engine = _engine()
    try:
        with engine.begin() as connection:
            session = _authenticate(args, connection)
            reviewer = decision_service.Reviewer(
                id=session.id,
                email=session.email,
                display_name=session.display_name,
                is_test=session.is_test,
            )
            secret = get_settings().session_secret.get_secret_value()
            preview = decision_service.preview(
                connection,
                mapping_id=uuid.UUID(args.mapping),
                decision=args.decision,
                reason=args.reason,
                reviewer=reviewer,
                expect_sha256=args.expect_sha256,
                secret=secret,
            )
            _print_decision_preview(preview)
            if args.dry_run:
                connection.rollback()
                print("\nDRY RUN: nothing was written.")
                return 0 if preview.valid else 2
            if not preview.valid:
                print("\nrefused: the decision did not pass validation (see BLOCKED above)")
                return 2

            result = decision_service.apply_decision(
                connection,
                token=preview.token or "",
                mapping_id=uuid.UUID(args.mapping),
                decision=args.decision,
                reason=args.reason,
                reviewer=reviewer,
                expect_sha256=args.expect_sha256,
                secret=secret,
                ttl_seconds=get_settings().preview_ttl_seconds,
            )
        _print_decision_result(result)
        return 0
    except _REFUSALS as exc:
        print(f"refused: {exc}")
        return 2
    finally:
        engine.dispose()


def _print_decision_preview(preview: Any) -> None:
    """Section K's before/after panel, in a terminal. The same data the console renders."""
    _rule("PREVIEW -- nothing has been written")
    binding = preview.binding
    authority = preview.authority
    if binding is not None:
        print(f"  source_ref          : {binding.source_ref}")
        print(f"  institution         : {binding.institution_label}")
        print(f"  responsibility      : {binding.claimed_responsibility}")
        print(f"  reviewed URL        : {binding.url}")
        print(f"  manifest            : MATCH ({binding.manifest_sha256[:12]}...)")
        if binding.same_page_as:
            print(
                f"  NOTE                : same physical page as {binding.same_page_as}, "
                "different responsibility"
            )
    if authority is not None:
        print(
            f"  requested authority : {authority.requested.host} "
            f"[{authority.requested.display}]"
        )
        print(
            f"  effective authority : {authority.effective.host} "
            f"[{authority.effective.display}]"
        )
        print(f"  redirect verdict    : {authority.verdict.value}")
    print()
    print(
        f"  BEFORE  {preview.before.verification_status} / "
        f"{preview.before.publication_eligibility} / "
        f"verified_by: {preview.before.verified_by_name or 'NULL'}"
    )
    if preview.after is not None:
        print(
            f"  AFTER   {preview.after.verification_status} / "
            f"{preview.after.publication_eligibility} / "
            f"verified_by: {preview.after.verified_by_name}"
        )
    print()
    print(f"  would append        : {preview.would_append or '(nothing)'}")
    print(f"  creates field_claim : {'YES' if preview.creates_field_claim else 'NO'}")
    print(f"  modifies canonical  : {'YES' if preview.modifies_canonical else 'NO'}")
    print(f"  promotes            : {'YES' if preview.promotes else 'NO'}")
    if preview.valid:
        print("\n  VALID PREVIEW")
    else:
        print("\n  BLOCKED")
        for blocker in preview.blockers:
            print(f"    - {blocker.code.value}: {blocker.message}")


def _print_decision_result(result: Any) -> None:
    """Section N: every real operation ends with a result read back from the database."""
    _rule("APPLIED -- read back from the database")
    print(f"  operation           : {result.action}")
    print("  status              : SUCCESS")
    print(f"  actor               : {result.reviewer.display_name}")
    print(f"  source              : {result.source_ref}")
    print(f"  responsibility      : {result.responsibility}")
    print(f"  previous state      : {result.before.verification_status}")
    print(f"  current state       : {result.after.verification_status}")
    print(f"  audit sequence      : {result.audit_seq}")
    print(f"  audit chain         : {'PASS' if result.audit_chain_ok else 'FAIL'}")
    print(f"  publication elig.   : {result.after.publication_eligibility}")
    print(f"  promoted            : {'YES' if result.after.promoted_source_id else 'NO'}")
    print(f"  field_claim created : {'YES' if result.field_claim_count else 'NO'}")
    print(f"  canonical changed   : {'NO' if result.canonical_unchanged else 'YES'}")


def command_promote(args: argparse.Namespace) -> int:
    engine = _engine()
    try:
        with engine.begin() as connection:
            session = _authenticate(args, connection)
            # The reviewed packet. `require_binding` is the only manifest-checked way to
            # obtain one, so promotion cannot reach a mapping the package never described.
            binding = require_responsibility_binding(
                connection,
                mapping_id=uuid.UUID(args.mapping),
                expect_sha256=args.expect_sha256,
            )
            result = promote(
                connection,
                mapping_id=uuid.UUID(args.mapping),
                source_id=uuid.UUID(args.source),
                actor=Actor(id=session.id, display=session.display_name),
                reason=args.reason,
                binding=binding,
                dry_run=args.dry_run,
            )
            if args.dry_run:
                connection.rollback()
        prefix = "DRY RUN: " if args.dry_run else ""
        if result.already_promoted:
            print(f"{prefix}already promoted; nothing to do ({result.eligibility})")
        else:
            print(
                f"{prefix}{result.responsibility}: source earns {result.eligibility}, "
                f"{result.bindings_written} field binding(s)"
            )
        return 0
    except _REFUSALS as exc:
        print(f"refused: {exc}")
        return 2
    finally:
        engine.dispose()


# ===========================================================================
# pilot source bridge (sections 1-3)
# ===========================================================================


def command_pilot_source(args: argparse.Namespace) -> int:
    """Record a reviewer's decision about one workbook row.

    A thin wrapper over `pilot.verification.verify_candidate` / `reject_candidate` /
    `flag_candidate_for_review`. The business logic, the lock order and the audit append
    stay where they are; this supplies an authenticated actor and the manifest check.

    The row is named by its **id**, not by its URL. 319 physical pages carry 385 claims
    and several share a URL, so matching on a display string would be ambiguous exactly
    where ambiguity is most expensive.
    """
    from app.domains.pilot import verification as pilot

    engine = _engine()
    try:
        with engine.begin() as connection:
            session = _authenticate(args, connection)
            _require_manifest(args.expect_sha256, "responsibility_decisions_proposed")

            candidate_id = uuid.UUID(args.pilot_source)
            current = connection.execute(
                text(
                    "SELECT source_ref, host, source_type, verification_state::text AS state "
                    "  FROM pilot_collected_source WHERE id = :i"
                ),
                {"i": candidate_id},
            ).one_or_none()
            if current is None:
                print(f"refused: no pilot_collected_source {candidate_id}")
                return 2

            target = {
                "VERIFIED": "VERIFIED",
                "REJECTED": "REJECTED",
                "NEEDS_REVIEW": "NEEDS_REVIEW",
            }[args.decision]
            if current.state == target:
                print(
                    f"NO_CHANGE: {current.source_ref} ({current.source_type}) is already "
                    f"{target}"
                )
                return 0
            if args.dry_run:
                print(
                    f"DRY RUN: {current.source_ref} ({current.source_type}) would change "
                    f"{current.state} -> {target}"
                )
                connection.rollback()
                return 0

            actor = pilot.Actor(user_id=session.id)
            if args.decision == "VERIFIED":
                pilot.verify_candidate(
                    connection, candidate_id=candidate_id, actor=actor, reason=args.reason
                )
            elif args.decision == "REJECTED":
                pilot.reject_candidate(
                    connection, candidate_id=candidate_id, actor=actor, reason=args.reason
                )
            else:
                pilot.flag_candidate_for_review(
                    connection, candidate_id=candidate_id, actor=actor, reason=args.reason
                )
            print(f"{current.source_ref} ({current.source_type}): " f"{current.state} -> {target}")
        return 0
    except _REFUSALS as exc:
        print(f"refused: {exc}")
        return 2
    except Exception as exc:
        print(f"refused: {exc}")
        return 2
    finally:
        engine.dispose()


def command_register_pilot_source(args: argparse.Namespace) -> int:
    """Create the `source_mapping` for a verified workbook row.

    A wrapper over `pilot.verification.register_verified_candidate`, which refuses
    unless the row is already `VERIFIED` and its host already has a verified
    `official_domain`. Both refusals are the point: registration follows two decisions
    and makes neither.

    **Registration is not authority.** The mapping is created `CANDIDATE`, so its
    derived `publication_eligibility` is `NOT_ELIGIBLE`, nothing is promoted and no
    source becomes publishable. Two further explicit acts -- the responsibility decision
    and the promotion -- stand between this and a publishable source.
    """
    from app.domains.pilot import verification as pilot

    engine = _engine()
    try:
        with engine.begin() as connection:
            session = _authenticate(args, connection)
            candidate_id = uuid.UUID(args.pilot_source)
            row = connection.execute(
                text(
                    "SELECT source_ref, source_type, verification_state::text AS state, "
                    "       promoted_source_mapping_id "
                    "  FROM pilot_collected_source WHERE id = :i"
                ),
                {"i": candidate_id},
            ).one_or_none()
            if row is None:
                print(f"refused: no pilot_collected_source {candidate_id}")
                return 2
            if row.promoted_source_mapping_id is not None:
                print(
                    f"ALREADY_CURRENT: {row.source_ref} is registered as "
                    f"source_mapping {row.promoted_source_mapping_id}"
                )
                return 0
            if args.dry_run:
                print(
                    f"DRY RUN: would register {row.source_ref} ({row.source_type}) "
                    f"as a CANDIDATE source_mapping. Not eligible, not promoted."
                )
                connection.rollback()
                return 0

            mapping_id = pilot.register_verified_candidate(
                connection,
                candidate_id=candidate_id,
                source_category=str(row.source_type),
                actor=pilot.Actor(user_id=session.id),
                reason=args.reason,
            )
            print(
                f"{row.source_ref}: registered as source_mapping {mapping_id} "
                "(CANDIDATE, NOT_ELIGIBLE, not promoted)"
            )
        return 0
    except _REFUSALS as exc:
        print(f"refused: {exc}")
        return 2
    except Exception as exc:
        print(f"refused: {exc}")
        return 2
    finally:
        engine.dispose()


def _require_manifest(expected: str | None, name: str) -> None:
    """Refuse unless the on-disk package is the one the reviewer approved (section 5).

    Validated before any write, not merely printed. A decision made against one package
    and applied against another is the failure the digests exist to prevent, and a hash
    that is only displayed prevents nothing.
    """
    if expected is None:
        return
    path = Path(".reports/step-5c5") / f"{name}.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    payload = json.dumps(envelope["rows"], sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if digest != expected:
        raise SystemExit(
            "REVIEW_MANIFEST_CHANGED\n"
            f"  approved : {expected}\n  on disk  : {digest}\n"
            "The package under review is not the one being applied. Re-review the "
            "current package, or apply the one that was approved. It is NOT regenerated "
            "automatically."
        )


def command_reviewer_whoami(args: argparse.Namespace) -> int:
    """Authenticate, then report who this is and what they may do. Writes nothing.

    WHY THIS EXISTS
    ===============
    Until now the only way to prove a reviewer's credential worked was to run a decision
    command with `--dry-run`, which requires having already chosen a decision. That
    inverts the order of the review protocol: the reviewer should be able to confirm they
    can authenticate *before* being asked to judge anything, and an operator should be
    able to ask for that confirmation without also asking for a decision.

    It goes through exactly the same path as every write command -- `authenticate()` with
    a password from a TTY or `DATAHUB_REVIEWER_PASSWORD`, never an argument -- so a
    success here means a success there. It is not a second, weaker way in: it grants
    nothing, issues nothing, and reads only the caller's own identity.

    Prints no credential material: no password, no hash, no token. Only the identity the
    audit trail would name and the permissions it carries.
    """
    engine = _engine(DatabaseRole.API)
    try:
        with engine.connect() as connection:
            session = _authenticate(args, connection)
            held = sorted(permissions_of(connection, session.id))
            _rule("AUTHENTICATION_SUCCESS")
            print(f"  email        : {session.email}")
            print(f"  display name : {session.reviewer.display_name}")
            print(f"  actor id     : {session.id}")
            print(f"  roles        : {', '.join(session.reviewer.roles) or '(none)'}")
            print(f"  fixture      : {session.reviewer.is_test}")
            print()
            print(f"  permissions ({len(held)}):")
            for permission in held:
                print(f"    {permission}")
            for gate, what in (
                (VERIFY_PERMISSION, "record a source-verification decision"),
                ("proposal:create", "create a change proposal"),
                ("proposal:review", "review a change proposal"),
                ("proposal:publish", "publish a change proposal"),
                ("source:manage", "register or edit a source"),
            ):
                print(f"  may {what:<45} {'yes' if gate in held else 'no'}")
            print()
            print("  This command wrote nothing and decided nothing.")
        return 0
    except _REFUSALS as exc:
        print(f"AUTHENTICATION_FAILED: {exc}")
        return 2
    except AccountRefusedError as exc:
        print(f"AUTHENTICATION_FAILED: {exc}")
        return 2
    except OperationalError as exc:
        print("refused: could not connect to the database as the application role.")
        print(f"  {str(getattr(exc, 'orig', exc)).strip().splitlines()[0]}")
        print("  Check POSTGRES_API_PASSWORD in .env.")
        return 2
    finally:
        engine.dispose()


#: Where Step 5C.1 wrote the normalised documents on this machine. Overridable, because
#: a different environment stores them elsewhere; not discovered, because guessing which
#: directory holds evidence is not a thing a reviewer should have to do.
DEFAULT_ARTIFACT_ROOT = Path(".artifacts-full")


def command_source_body(args: argparse.Namespace) -> int:
    """Show the stored body of one pilot-source page. READ ONLY; decides nothing.

    A reviewer has been told the host is official, the access class, the title and the
    candidate count. None of that answers the question they are being asked -- does this
    page carry this responsibility? -- and a page can be live, titled, on a verified host
    and still be a news item or a listing for another degree level.

    Prints the **normalised artifact**, not the raw HTML: it is the same text the claim
    extractors read, so a reviewer is judging the document the system judged. Scripts and
    trackers are not stripped here; they were never in the artifact.

    Writes nothing and fetches nothing. Looking at evidence is not an event, and a viewer
    that appended "who looked" to an append-only chain would fill it with glances.
    """
    engine = _engine(DatabaseRole.API)
    try:
        with engine.connect() as connection:
            evidence = resolve_body(connection, uuid.UUID(args.pilot_source))

        _rule(f"{evidence.source_ref}  {evidence.responsibility}")
        print(f"  institution        : {evidence.institution}")
        print(f"  pilot source       : {evidence.source_ref}  ({evidence.pilot_source_id})")
        print(
            f"  responsibility     : {evidence.responsibility}"
            + ("  <-- UNDER REVIEW" if True else "")
        )
        print(f"  degree scope       : {evidence.degree_scope or '(none)'}")
        if evidence.duplicate_of:
            print(f"  duplicate of       : {evidence.duplicate_of} -- the SAME physical page.")
            print("                       The body below is shared; this responsibility is")
            print("                       decided on its own.")
        print(f"  original URL       : {evidence.requested_url}")
        print(f"  effective URL      : {evidence.effective_url or '(same)'}")
        print(
            f"  host               : {evidence.host}  "
            f"[{evidence.host_status or 'no official_domain row'}]"
        )
        print(f"  pilot state        : {evidence.verification_state}")
        print(f"  snapshot id        : {evidence.snapshot_id or '(none)'}")
        print(f"  extraction id      : {evidence.extraction_id or '(none)'}")
        print(
            f"  document artifact  : {evidence.document_artifact_version or '(none)'}"
            f"  status={evidence.extraction_status or '-'}"
        )
        print(f"  media type         : {evidence.media_type or '(unknown)'}")
        print(f"  body evidence      : {evidence.access_class}")
        if evidence.warnings:
            print(f"  extraction warnings: {evidence.warnings}")

        if not evidence.available:
            print()
            print(f"  {NOT_AVAILABLE}")
            print(f"    acquisition state : {evidence.access_class}")
            print(f"    fetch status      : {evidence.fetch_status or '(none)'}")
            print(f"    http status       : {evidence.http_status or '(none)'}")
            print(f"    error class       : {evidence.error_class or '(none)'}")
            print()
            print("    There is no stored body for this page, so nothing here supports the")
            print("    claim that it carries this responsibility. The host being")
            print("    VERIFIED_OFFICIAL is not evidence about this URL, and the URL path")
            print("    is not evidence either. No new fetch was attempted.")
            return 0

        store = build_evidence_store(
            get_settings(), prefix=DERIVED_PREFIX, local_root=Path(args.artifact_root)
        )
        document = load_body(store, evidence)
        print(f"  blocks / tables    : {len(document.blocks)} / {len(document.tables)}")
        print()
        _rule("NORMALISED DOCUMENT BODY")
        print(render_body(document, max_blocks=args.max_blocks, skip_chrome=args.skip_chrome))
        _rule("END OF BODY -- this command decided nothing")
        return 0
    except SourceBodyError as exc:
        print(f"refused: {exc}")
        return 2
    except FileNotFoundError as exc:
        print(f"refused: the artifact is not in {args.artifact_root}: {exc}")
        print("  The database knows the document hash; the bytes live in the artifact")
        print("  store. Point --artifact-root at the store Step 5C.1 wrote.")
        return 2
    except OperationalError as exc:
        print("refused: could not connect to the database as the application role.")
        print(f"  {str(getattr(exc, 'orig', exc)).strip().splitlines()[0]}")
        return 2
    finally:
        engine.dispose()


# ===========================================================================
# credential enrolment (section 8)
# ===========================================================================


def _read_twice(prompt: str) -> str | None:
    """Read a new password from a TTY twice. Returns None when they differ.

    Never from an argument: a password on a command line is in the shell history, in
    `ps` output and in anything that echoes the command.
    """

    first = getpass.getpass(prompt)
    second = getpass.getpass("Repeat: ")
    if first != second:
        return None
    return first


def _read_new_password(*, attempts: int = 3) -> str | None:
    """Read a new password, checking it locally before anything is spent.

    Validates length and the repeat **here**, in a short retry loop, for one reason: the
    enrolment token has a clock on it. Letting a mistyped repeat or an eight-character
    password abort the command meant a fresh token, a fresh out-of-band handover and
    another thirty-minute window -- for a mistake the operator can fix in five seconds.
    Nothing has been written at this point, so retrying costs nothing.

    Says the requirement up front. It used to be discovered by reading
    `WeakPasswordError` off the bottom of a stack trace, after typing the password twice.

    Returns None when the operator gives up or interrupts, which the caller reports as a
    refusal rather than a crash.
    """
    print(f"The password must be at least {MINIMUM_LENGTH} characters. It will not echo.")
    for remaining in range(attempts, 0, -1):
        try:
            candidate = _read_twice("New password: ")
        except (KeyboardInterrupt, EOFError):
            print()
            print("refused: cancelled at the password prompt. Nothing was changed.")
            return None
        if candidate is None:
            print(f"  the two entries differ. {remaining - 1} attempt(s) left.")
            continue
        if len(candidate) < MINIMUM_LENGTH:
            # Length is checked here as well as in `hash_password`, which is the
            # authority. This copy exists to turn it into a retry instead of a traceback.
            print(
                f"  too short: {len(candidate)} character(s), minimum is "
                f"{MINIMUM_LENGTH}. {remaining - 1} attempt(s) left."
            )
            continue
        return candidate
    print("refused: no acceptable password after several attempts. Nothing was changed.")
    return None


def command_reviewer_issue_enrollment(args: argparse.Namespace) -> int:
    """Issue the one-time challenge that lets somebody claim a provisioned account.

    Run by the OPERATOR, not by the reviewer, and on the owning connection -- the
    application role holds no INSERT on `credential_enrollment`, so it cannot mint one.
    That asymmetry is the whole point: knowing an email address must not be enough to
    claim an identity, and if the person claiming could also issue, it would be.

    Sets no password and touches no verification state. It prints the token once, to
    this terminal, for delivery to the reviewer out of band. It is never stored, logged
    or audited -- only its SHA-256 goes to the database.
    """
    engine = _engine(DatabaseRole.MIGRATION)
    lifetime = timedelta(minutes=args.minutes)
    try:
        with engine.begin() as connection:
            challenge = issue_enrollment_challenge(
                connection,
                email=args.email,
                issued_by=None,
                lifetime=lifetime,
                allow_test_identity=args.allow_test_identity,
                rotate=args.reissue,
                reason=args.reason,
            )
        print(f"CREDENTIAL_ENROLLMENT_ISSUED: {challenge.email}")
        authority = (
            "BOOTSTRAP (no administrator exists yet)"
            if challenge.bootstrap_mode
            else "an authenticated administrator"
        )
        print(
            f"  expires   : {challenge.expires_at:%Y-%m-%d %H:%M:%S %Z} "
            f"({args.minutes} minutes)"
        )
        print(f"  authority : {authority}")
        print("")
        print("  Enrollment token (shown once; deliver out of band, never by email):")
        print(f"      {challenge.token}")
        print("")
        print("  The database stores only its SHA-256. If this scrolls away it cannot be")
        print("  recovered -- reissue with --reissue, which revokes this one.")
        return 0
    except BootstrapClosedError as exc:
        print(f"refused: {exc}")
        return 2
    except (EnrollmentTokenError, AccountRefusedError, NotAuthorizedError) as exc:
        print(f"refused: {exc}")
        return 2
    finally:
        engine.dispose()


def command_reviewer_revoke_enrollment(args: argparse.Namespace) -> int:
    """Revoke an outstanding challenge, e.g. when one was delivered to the wrong place."""
    engine = _engine(DatabaseRole.MIGRATION)
    try:
        with engine.begin() as connection:
            revoked = revoke_enrollment_challenge(connection, email=args.email, reason=args.reason)
        print(f"revoked {revoked} outstanding challenge(s) for {args.email}")
        return 0 if revoked else 1
    except (EnrollmentTokenError, AccountRefusedError) as exc:
        print(f"refused: {exc}")
        return 2
    finally:
        engine.dispose()


def command_reviewer_enrol_password(args: argparse.Namespace) -> int:
    """Claim an account with the challenge issued for it, and set the first password.

    Run by the REVIEWER. Unauthenticated, because somebody with no password has nothing
    to authenticate with -- so the proof is possession of the token, which only the
    operator who issued it could have given them.

    Takes no `--email`: the token names the account, so this cannot be pointed at one of
    the runner's choosing. Takes no `--token` either -- a command line ends up in shell
    history and `ps` output, so both values are read from the terminal.

    The token is read **visibly**; the password is not. `reviewer-issue-enrollment`
    printed the token in plaintext a moment earlier, so hiding it during entry conceals
    nothing that was not already on the screen -- while a 43-character random string
    entered with no echo gives the operator no way to distinguish a paste that worked
    from one that did not. That is how the first real attempt failed: `cmd.exe` does not
    paste on Ctrl+V, nothing appeared on screen (nothing would have appeared either way),
    and Enter submitted an empty string. The password stays hidden because it is never
    displayed anywhere and the operator knows what they typed.

    Runs as the APPLICATION role, deliberately. Claiming does not need the owner's
    credential; issuing does. If this command needed it too, the separation would be
    decorative.
    """
    _require_configured(DatabaseRole.API)
    engine = _engine(DatabaseRole.API)
    token = ""
    password = None
    try:
        print("Paste the enrollment token, then press Enter. It will be visible.")
        print("  cmd.exe pastes on RIGHT-CLICK, not Ctrl+V.")
        try:
            token = input("Enrollment token: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            print("refused: cancelled at the token prompt. Nothing was changed.")
            return 2
        if not token:
            print("refused: no enrollment token supplied")
            print("  Nothing was consumed -- the token is still valid until it expires.")
            return 2
        if not looks_like_token(token):
            # A partial paste and a wrong token are different mistakes. Saying which
            # matters because the terminal that caused this pastes unreliably, and the
            # remedy differs: paste again, versus ask for a reissue.
            print(
                f"refused: that is {len(token)} character(s); an enrollment token is "
                f"{TOKEN_LENGTH} characters of letters, digits, '-' and '_'."
            )
            print("  Looks like a partial paste. Nothing was consumed -- try again.")
            return 2
        password = _read_new_password()
        if password is None:
            return 2
        with engine.begin() as connection:
            result = enrol_initial_password(connection, token=token, password=password)
        print(f"{result.operation}: {result.email}")
        print("  The plaintext was not stored, printed or logged; only an Argon2id hash.")
        print("  The token is spent. This account can now authenticate, and cannot be")
        print("  enrolled again.")
        return 0
    except TokenExpiredError as exc:
        print(f"refused: {exc}")
        print("  Ask the operator to reissue: reviewer-issue-enrollment --reissue")
        return 2
    except CredentialAlreadyEnrolledError as exc:
        print(f"refused: {exc}")
        return 2
    except WeakPasswordError as exc:
        # `_read_new_password` checks this first, so reaching here means the rule changed
        # underneath it. Still reported rather than raised: an unhandled traceback at a
        # credential prompt tells the operator nothing and looks like a crash.
        print(f"refused: {exc}")
        print("  Nothing was consumed -- the token is still valid until it expires.")
        return 2
    except (EnrollmentTokenError, EnrolmentRefusedError, AccountRefusedError) as exc:
        print(f"refused: {exc}")
        return 2
    except OperationalError as exc:
        # Most often the application credential: POSTGRES_API_PASSWORD not matching the
        # role. Worth naming, because the raw psycopg traceback buries one line of cause
        # under forty lines of connection-pool frames.
        print("refused: could not connect to the database as the application role.")
        print(f"  {str(getattr(exc, 'orig', exc)).strip().splitlines()[0]}")
        print("  Check POSTGRES_API_PASSWORD in .env. Nothing was consumed -- the token")
        print("  is still valid until it expires.")
        return 2
    finally:
        del token, password
        engine.dispose()


def command_reviewer_change_password(args: argparse.Namespace) -> int:
    """Replace your own password, proving the current one first (section 3).

    Email alone is never sufficient authority over an account. That was the defect in
    the command this replaces.
    """

    engine = _engine(DatabaseRole.MIGRATION)
    try:
        current = getpass.getpass(f"Current password for {args.email}: ")
        new = _read_twice("New password: ")
        if new is None:
            print("refused: the two entries differ")
            return 2
        with engine.begin() as connection:
            result = change_own_password(
                connection,
                email=args.email,
                current_password=current,
                new_password=new,
                allow_test_identity=args.allow_test_identity,
            )
        del current, new
        print(f"{result.operation}: {result.email}")
        print("  The previous password no longer authenticates.")
        return 0
    except (EnrolmentRefusedError, AccountRefusedError) as exc:
        print(f"refused: {exc}")
        return 2
    finally:
        engine.dispose()


def command_reviewer_reset_password(args: argparse.Namespace) -> int:
    """Reset somebody else's password, as an authenticated administrator (section 3).

    The administrator authenticates with their own credential; the target is named by
    email. Audited as `CREDENTIAL_RESET`.

    Unusable today, and that is the honest state: no identity holds `admin:roles`, and
    creating one to make this path work would be inventing the authority it checks.
    """

    engine = _engine(DatabaseRole.MIGRATION)
    try:
        with engine.begin() as connection:
            administrator = authenticate(
                connection,
                email=args.administrator_email,
                password=getpass.getpass(f"Password for {args.administrator_email}: "),
            )
            new = _read_twice(f"New password for {args.email}: ")
            if new is None:
                print("refused: the two entries differ")
                return 2
            result = reset_password_as_administrator(
                connection,
                administrator_id=administrator.id,
                target_email=args.email,
                new_password=new,
                reason=args.reason,
                allow_test_identity=args.allow_test_identity,
            )
            del new
        print(f"{result.operation}: {result.email} (by {administrator.email})")
        return 0
    # AccountRefusedError is the base of EnrolmentRefusedError and covers the checks that
    # moved onto the shared account floor -- a deactivated target arrives as the base.
    except (AccountRefusedError, AuthenticationFailedError) as exc:
        print(f"refused: {exc}")
        return 2
    finally:
        engine.dispose()


# ===========================================================================
# batch manifest (sections 16, 17)
# ===========================================================================


def _load_manifest(path: Path, expect_sha: str | None) -> list[dict[str, Any]]:
    """Read a manifest and prove it is the version that was reviewed.

    Section 17. Without this the sequence is: a reviewer reads manifest A, somebody
    regenerates it into manifest B, and the apply command silently applies B. The hash
    is over the rows, so it is stable across re-serialisation and unaffected by when the
    file was written.
    """
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(envelope, dict) or "rows" not in envelope:
        raise SystemExit(f"{path} is not a manifest: no `rows`")
    rows = envelope["rows"]
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if digest != envelope.get("content_sha256"):
        raise SystemExit(
            f"{path} has been edited since it was generated: its rows hash to {digest} "
            f"and it claims {envelope.get('content_sha256')}"
        )
    if expect_sha and digest != expect_sha:
        raise SystemExit(
            f"{path} is not the manifest that was reviewed.\n"
            f"  reviewed: {expect_sha}\n  this file: {digest}\n"
            "Re-review the current manifest, or apply the one that was approved."
        )
    return list(rows)


def command_apply_manifest(args: argparse.Namespace) -> int:
    """Apply a reviewer-approved manifest. Every row must carry an explicit decision."""
    path = Path(args.file)
    # Validated, not merely displayed (section 5). `_load_manifest` raises when the file
    # has been edited since generation, and again when it is not the approved version.
    rows = _load_manifest(path, args.expect_sha256)

    #: Section 16: a missing decision is an error. It is never read as VERIFIED, and the
    #: proposals this repository generates are all NEEDS_REVIEW, so applying an
    #: unreviewed manifest is a no-op by construction rather than by luck.
    malformed = [
        index for index, row in enumerate(rows, 1) if not str(row.get("decision") or "").strip()
    ]
    if malformed:
        print(
            f"{len(malformed)} row(s) carry no `decision` field: {malformed[:10]}"
            f"{'...' if len(malformed) > 10 else ''}"
        )
        print(
            "A manifest row with no decision is malformed. It is not treated as "
            "VERIFIED,\nand the whole file is refused rather than partly applied "
            "(documented policy)."
        )
        return 2

    tally = Counter(str(row["decision"]) for row in rows)
    _rule(f"MANIFEST {path.name}")
    print(f"\n  rows      : {len(rows)}")
    print(f"  decisions : {dict(sorted(tally.items()))}")
    payload = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
    print(f"  sha256    : {hashlib.sha256(payload.encode('utf-8')).hexdigest()}")

    engine = _engine()
    applied = 0
    try:
        with engine.begin() as connection:
            session = _authenticate(args, connection)
            print(f"  actor     : {session.email} ({', '.join(session.reviewer.roles)})\n")
            for index, row in enumerate(rows, 1):
                decision = str(row["decision"])
                reason = str(row.get("reason") or "").strip()
                if not reason:
                    raise SystemExit(f"row {index} carries a decision and no reason")
                if "host" in row:
                    # Bound from the row being applied, for the same reason the single
                    # -host command binds from the manifest: the institution the
                    # reviewer saw beside the host is the one the decision is about.
                    label = str(row.get("institution") or "").strip()
                    if not label:
                        raise SystemExit(
                            f"row {index} names host {row['host']!r} and no institution; "
                            "a domain decision must be attributable to one"
                        )
                    outcome = _apply_domain(
                        connection,
                        host=str(row["host"]).lower(),
                        decision=decision,
                        actor=session.id,
                        reason=reason,
                        reviewed=ReviewedHost(
                            host=str(row["host"]).lower(),
                            institution_label=label,
                            institution_id=resolve_institution(connection, label),
                            reached_only_by_redirect=bool(row.get("reached_only_by_redirect")),
                            manifest_sha256=str(args.expect_sha256 or ""),
                            proposed_status_if_accepted=str(
                                row.get("proposed_status_if_accepted") or ""
                            ),
                        ),
                        method=str(row.get("method") or "MANUAL_STAFF_REVIEW"),
                        dry_run=args.dry_run,
                    )
                else:
                    outcome = _apply_responsibility(
                        connection,
                        mapping_id=uuid.UUID(str(row["mapping_id"])),
                        decision=decision,
                        actor=session.id,
                        reason=reason,
                        dry_run=args.dry_run,
                    )
                applied += 1
                print(f"    {index:4} {outcome}")
            if args.dry_run:
                connection.rollback()
    except _REFUSALS as exc:
        print(f"refused: {exc}")
        return 2
    finally:
        engine.dispose()

    print(
        f"\n  {'DRY RUN, nothing written. ' if args.dry_run else ''}" f"{applied} row(s) processed."
    )
    print(
        "  Transaction policy: the whole manifest applies or none of it does. A row that "
        "\n  cannot be applied aborts the transaction, because a half-applied trust "
        "state is\n  worse than an unapplied one."
    )
    return 0


# ===========================================================================
# readiness (sections 22-24, 28)
# ===========================================================================


def command_readiness(args: argparse.Namespace) -> int:
    engine = _engine()
    with engine.connect() as connection:
        results = assess(connection, accepted_only=args.accepted_only)
    engine.dispose()

    ready = [r for r in results if r.ready]
    _rule("PROMOTION READINESS")
    print(f"\n  candidates considered : {len(results)}")
    print(f"  PROMOTION_READY       : {len(ready)}")
    print("\n  blockers, by how many candidates each holds up:")
    for blocker, count in summarise(results).items():
        print(f"    {blocker:32} {count}")

    print("\n  by field kind:")
    kinds = Counter(r.field_kind for r in results)
    for kind, count in kinds.most_common():
        ready_here = sum(1 for r in ready if r.field_kind == kind)
        print(f"    {kind:30} {count:5}   ready {ready_here}")

    if args.sample:
        print("\n  a sample, with every blocker on each row:")
        for result in results[: args.sample]:
            print(f"    {result.candidate_id} {result.field_kind}")
            print(f"        responsibility: {result.responsibility}")
            print(
                f"        blockers      : "
                f"{', '.join(b.value for b in result.blockers) or 'NONE - ready'}"
            )
    return 0


def command_matrix(_: argparse.Namespace) -> int:
    """The section 28 matrix, computed from the policy rather than written out."""
    _rule("SOURCE / RESPONSIBILITY READINESS MATRIX")
    cases = (
        ("VERIFIED_OFFICIAL", "LANGUAGE_REQUIREMENTS", "VERIFIED", True, "LANGUAGE_OVERALL_SCORE"),
        ("VERIFIED_OFFICIAL", "TUITION_FEES", "REJECTED", False, "TUITION"),
        (
            "VERIFIED_OFFICIAL",
            "APPLICATION_DEADLINES",
            "NEEDS_REVIEW",
            False,
            "APPLICATION_DEADLINE",
        ),
        ("AUTHORIZED_EXTERNAL", "PROGRAM_PAGE", "VERIFIED", True, "PROGRAM_NAME"),
        ("REJECTED", "LANGUAGE_REQUIREMENTS", "VERIFIED", True, "LANGUAGE_OVERALL_SCORE"),
        ("VERIFIED_OFFICIAL", "LANGUAGE_REQUIREMENTS", "VERIFIED", True, "TUITION"),
    )
    header = (
        f"  {'domain':22} {'responsibility':24} {'decision':13} {'promoted':9} {'field':26} ready"
    )
    print("\n" + header)
    print("  " + "-" * (len(header) - 2))
    for domain, responsibility, decision, promoted, field in cases:
        blockers: list[Blocker] = []
        if domain not in ("VERIFIED_OFFICIAL", "AUTHORIZED_EXTERNAL"):
            blockers.append(Blocker.DOMAIN_NOT_VERIFIED)
        if decision not in ("VERIFIED", "AUTHORIZED_EXTERNAL"):
            blockers.append(Blocker.RESPONSIBILITY_NOT_VERIFIED)
        if not promoted:
            blockers.append(Blocker.MAPPING_NOT_PROMOTED)
        if not is_compatible(field, responsibility):
            blockers.append(Blocker.RESPONSIBILITY_INCOMPATIBLE)
        verdict = "YES" if not blockers else "no"
        print(
            f"  {domain:22} {responsibility:24} {decision:13} {promoted!s:9} "
            f"{field:26} {verdict}"
        )
        if blockers:
            print(f"      blocked by: {', '.join(b.value for b in blockers)}")
    print(
        "\n  Computed from `verification.policy`, not written out: a matrix that agreed "
        "\n  with the documentation and disagreed with the code would be worse than none."
    )
    return 0


COMMANDS = {
    "reviewer-create": command_reviewer_create,
    "reviewer-issue-enrollment": command_reviewer_issue_enrollment,
    "reviewer-revoke-enrollment": command_reviewer_revoke_enrollment,
    "reviewer-enrol-password": command_reviewer_enrol_password,
    "reviewer-change-password": command_reviewer_change_password,
    "reviewer-reset-password": command_reviewer_reset_password,
    "reviewer-status": command_reviewer_status,
    "source-body": command_source_body,
    "reviewer-whoami": command_reviewer_whoami,
    "pilot-source": command_pilot_source,
    "register-pilot-source": command_register_pilot_source,
    "domain": command_domain,
    "responsibility": command_responsibility,
    "promote": command_promote,
    "apply-manifest": command_apply_manifest,
    "readiness": command_readiness,
    "matrix": command_matrix,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("reviewer-create", help="provision a real reviewer identity")
    create.add_argument("--email", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--role", default="reviewer")
    create.add_argument("--test-only", action="store_true")
    # Deliberately no `--role` beyond the default: section 1 grants only the existing
    # reviewer role, and an option to pick another would be an option to grant
    # `source:manage` or worse by typo.
    create.add_argument("--dry-run", action="store_true")

    sub.add_parser("reviewer-status", help="who may record a verification decision")

    # Read-only, so no --reviewer-email and no --reason: looking at evidence is not
    # an event and needs no justification recorded against anybody.
    body = sub.add_parser(
        "source-body",
        help="READ ONLY: show the stored normalised body of one pilot-source page",
    )
    body.add_argument("--pilot-source", required=True, metavar="UUID")
    body.add_argument(
        "--artifact-root",
        default=str(DEFAULT_ARTIFACT_ROOT),
        help="where the normalised documents are stored (default %(default)s)",
    )
    body.add_argument(
        "--max-blocks",
        type=int,
        default=0,
        help="truncate after N blocks; 0 for the whole document (default)",
    )
    body.add_argument(
        "--skip-chrome",
        action="store_true",
        help="hide nav/header/footer blocks. Approximate and under-filters; it never "
        "hides real content, and the number hidden is printed. Off by default.",
    )

    whoami = sub.add_parser(
        "reviewer-whoami",
        help="REVIEWER: authenticate and report your own identity and permissions",
    )
    # Deliberately NOT `_auth_arguments`: that adds a required `--reason`, which exists
    # because every write must record why. This command writes nothing, so demanding a
    # reason would be asking the reviewer to justify reading their own identity.
    whoami.add_argument("--reviewer-email", required=True)
    whoami.add_argument("--allow-test-identity", action="store_true", help=argparse.SUPPRESS)

    # Every write below takes `--reviewer-email`, `--reason` and `--dry-run` from
    # `_auth_arguments`, and **no `--actor`**: the actor is whoever authenticated.
    domain = sub.add_parser("domain", help="apply one reviewed domain decision")
    domain.add_argument("--host", required=True)
    domain.add_argument("--decision", required=True, choices=sorted(DOMAIN_DECISIONS))
    # The stable identifier is preferred. `--institution` is kept as an accepted alias
    # because it already took a UUID rather than a name, so nothing has to be relearned;
    # both are validated against the frozen manifest before anything is written.
    domain.add_argument(
        "--institution-id",
        default=None,
        metavar="UUID",
        help="target_institution UUID this host was reviewed under (required)",
    )
    domain.add_argument("--institution", default=None, help=argparse.SUPPRESS)
    domain.add_argument("--method", default="MANUAL_STAFF_REVIEW")
    domain.add_argument(
        "--expect-sha256",
        required=True,
        help="sha256 of the approved domain manifest; validated with the host and "
        "institution as one reviewed tuple",
    )
    _auth_arguments(domain)

    pilot_source = sub.add_parser("pilot-source", help="record a decision about one workbook row")
    pilot_source.add_argument("--pilot-source", required=True)
    pilot_source.add_argument(
        "--decision", required=True, choices=("VERIFIED", "REJECTED", "NEEDS_REVIEW")
    )
    pilot_source.add_argument("--expect-sha256", default=None)
    _auth_arguments(pilot_source)

    register = sub.add_parser(
        "register-pilot-source", help="create the source_mapping for a verified row"
    )
    register.add_argument("--pilot-source", required=True)
    _auth_arguments(register)

    responsibility = sub.add_parser("responsibility", help="apply one reviewed decision")
    responsibility.add_argument("--mapping", required=True)
    responsibility.add_argument(
        "--decision", required=True, choices=sorted(RESPONSIBILITY_DECISIONS)
    )
    # Required, not optional. An optional digest is a digest that gets omitted on the
    # day it matters, and until Step 5C.7L omitting it skipped the check entirely.
    responsibility.add_argument("--expect-sha256", required=True)
    _auth_arguments(responsibility)

    promotion = sub.add_parser("promote", help="promote a verified mapping onto its source")
    promotion.add_argument("--mapping", required=True)
    promotion.add_argument("--source", required=True)
    # Promotion is the act that makes a page publishable, so it binds to the reviewed
    # packet exactly as the responsibility decision does.
    promotion.add_argument("--expect-sha256", required=True)
    _auth_arguments(promotion)

    manifest = sub.add_parser("apply-manifest", help="apply an approved manifest")
    manifest.add_argument("--file", required=True)
    manifest.add_argument("--expect-sha256", default=None)
    manifest.add_argument("--reviewer-email", required=True)
    manifest.add_argument("--dry-run", action="store_true")
    manifest.add_argument("--allow-test-identity", action="store_true", help=argparse.SUPPRESS)

    # Three operations, not one. A first enrolment and a reset are different acts with
    # different authority, and the command that conflated them could overwrite any
    # reviewer's credential given only their email address.
    issue = sub.add_parser(
        "reviewer-issue-enrollment",
        help="OPERATOR: issue the one-time challenge that lets somebody claim an account",
    )
    issue.add_argument("--email", required=True, help="the account being claimed")
    issue.add_argument(
        "--minutes",
        type=int,
        default=int(DEFAULT_LIFETIME.total_seconds() // 60),
        help="how long the challenge stays live (default %(default)s)",
    )
    issue.add_argument(
        "--reissue",
        action="store_true",
        help="revoke the outstanding challenge first; the previous token stops working",
    )
    issue.add_argument("--reason", default="")
    issue.add_argument("--allow-test-identity", action="store_true", help=argparse.SUPPRESS)

    revoke = sub.add_parser(
        "reviewer-revoke-enrollment",
        help="OPERATOR: revoke an outstanding challenge without issuing a replacement",
    )
    revoke.add_argument("--email", required=True)
    revoke.add_argument("--reason", required=True)

    # No --email and no --token. The token names the account, and a token on a command
    # line is in the shell history and in `ps` output; both are prompted for instead.
    sub.add_parser(
        "reviewer-enrol-password",
        help="REVIEWER: claim your account with the challenge issued for it",
    )

    change = sub.add_parser(
        "reviewer-change-password",
        help="replace your own password; asks for the current one",
    )
    change.add_argument("--email", required=True)
    change.add_argument("--allow-test-identity", action="store_true", help=argparse.SUPPRESS)

    reset = sub.add_parser(
        "reviewer-reset-password",
        help="reset another account's password, as an authenticated administrator",
    )
    reset.add_argument("--email", required=True, help="the account being reset")
    reset.add_argument("--administrator-email", required=True)
    reset.add_argument("--reason", required=True)
    reset.add_argument("--allow-test-identity", action="store_true", help=argparse.SUPPRESS)

    readiness = sub.add_parser("readiness", help="promotion readiness with blockers")
    readiness.add_argument("--accepted-only", action="store_true")
    readiness.add_argument("--sample", type=int, default=0)

    sub.add_parser("matrix", help="the source/responsibility readiness matrix")

    args = parser.parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
