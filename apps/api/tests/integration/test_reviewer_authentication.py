"""Authentication, actor binding and the reviewer/publisher split (Step 5C.7B).

THE DEFECT THESE CLOSE
======================
A write used to take `--actor <uuid>`. That is attribution, not authentication: anyone
with CLI access could type somebody else's id and the audit log would name a person who
had not made the decision. In a system whose entire purpose is that every published fact
carries a named reviewer, that was the weakest link in the chain.

Every test here is one way the fix could stop working without anyone noticing: a
credential that verifies when it should not, an actor that is accepted from an argument,
a deactivated account that still authenticates, a fixture identity that reaches real
data, or the reviewer role quietly regaining publication authority.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Connection, text

from app.domains.identity.passwords import (
    MINIMUM_LENGTH,
    WeakPasswordError,
    hash_password,
    verify_password,
)
from app.domains.verification.authentication import (
    ActorMismatchError,
    AuthenticationFailedError,
    FixtureIdentityRefusedError,
    authenticate,
    refuse_test_identity_on_real_data,
)
from app.domains.verification.identity import (
    GRANT_PERMISSION,
    VERIFY_PERMISSION,
    BootstrapClosedError,
    NotAuthorizedError,
    authorize_grant,
    permissions_of,
    provision_reviewer,
)

pytestmark = pytest.mark.integration

PASSWORD = "a-long-enough-fixture-password"
OTHER_PASSWORD = "a-different-long-password"


def _enrol(
    conn: Connection,
    *,
    role: str = "reviewer",
    password: str | None = PASSWORD,
    test_only: bool = True,
    active: bool = True,
    granted_by: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:
    """A fixture identity with a real Argon2id credential."""
    unique = uuid.uuid4().hex[:8]
    email = f"auth-{unique}@example.test"
    reviewer = provision_reviewer(
        conn,
        email=email,
        display_name=f"Auth fixture {unique}",
        role=role,
        test_only=test_only,
        granted_by=granted_by,
    )
    if password is not None:
        conn.execute(
            text("UPDATE app_user SET password_hash = :h WHERE id = :i"),
            {"h": hash_password(password), "i": reviewer.id},
        )
    if not active:
        # `ck_app_user_deactivation_has_a_timestamp`: a deactivated account has to say
        # when, so "is it off?" and "since when?" cannot disagree.
        conn.execute(
            text("UPDATE app_user SET is_active = false, deactivated_at = now() WHERE id = :i"),
            {"i": reviewer.id},
        )
    return reviewer.id, email


# ===========================================================================
# The credential itself
# ===========================================================================


def test_the_stored_hash_is_argon2id_as_the_model_says() -> None:
    """`AppUser.password_hash` is documented as Argon2id. It has to actually be one."""
    digest = hash_password(PASSWORD)
    assert digest.startswith("$argon2id$"), digest[:20]
    assert PASSWORD not in digest


def test_a_password_below_the_floor_is_refused() -> None:
    with pytest.raises(WeakPasswordError, match=str(MINIMUM_LENGTH)):
        hash_password("short")


def test_verification_is_not_fooled_by_an_absent_or_wrong_credential() -> None:
    digest = hash_password(PASSWORD)
    assert verify_password(digest, PASSWORD).matched
    assert not verify_password(digest, OTHER_PASSWORD).matched
    assert not verify_password(None, PASSWORD).matched, "no hash must not mean any password"
    assert not verify_password("not-a-hash", PASSWORD).matched


# ===========================================================================
# 11. Actor spoofing
# ===========================================================================


def test_the_authenticated_reviewer_may_act_as_itself(conn: Connection) -> None:
    actor, email = _enrol(conn)
    session = authenticate(conn, email=email, password=PASSWORD)
    assert session.id == actor
    assert session.method == "local-password"
    assert session.bind(actor) == actor
    assert session.bind(None) == actor, "no claim means the session's own identity"


def test_an_authenticated_reviewer_cannot_act_as_another(conn: Connection) -> None:
    """The case `--actor` allowed and authentication closes."""
    _, email = _enrol(conn)
    other, _ = _enrol(conn)
    session = authenticate(conn, email=email, password=PASSWORD)
    with pytest.raises(ActorMismatchError, match="attributed to whoever made it"):
        session.bind(other)


def test_an_anonymous_caller_cannot_borrow_a_reviewer_id(conn: Connection) -> None:
    """There is no path from a bare UUID to a session. Authentication is the only one."""
    actor, email = _enrol(conn)
    with pytest.raises(AuthenticationFailedError):
        authenticate(conn, email=email, password="")
    with pytest.raises(AuthenticationFailedError):
        authenticate(conn, email=f"not-{email}", password=PASSWORD)
    assert isinstance(actor, uuid.UUID)


def test_an_invalid_credential_is_refused(conn: Connection) -> None:
    _, email = _enrol(conn)
    with pytest.raises(AuthenticationFailedError):
        authenticate(conn, email=email, password=OTHER_PASSWORD)


def test_an_account_with_no_password_cannot_authenticate(conn: Connection) -> None:
    """Every real account is in this state until its owner enrols a credential."""
    _, email = _enrol(conn, password=None)
    with pytest.raises(AuthenticationFailedError, match="no account with that email"):
        authenticate(conn, email=email, password=PASSWORD)


def test_a_deactivated_reviewer_is_refused(conn: Connection) -> None:
    _, email = _enrol(conn, active=False)
    with pytest.raises(AuthenticationFailedError, match="deactivated"):
        authenticate(conn, email=email, password=PASSWORD)


def test_an_authenticated_user_without_the_permission_is_refused(conn: Connection) -> None:
    """Proving who you are is not the same as being allowed to decide."""
    _, email = _enrol(conn, role="data_editor")
    with pytest.raises(AuthenticationFailedError, match=VERIFY_PERMISSION):
        authenticate(conn, email=email, password=PASSWORD)


def test_a_test_identity_authenticates_but_may_not_touch_real_data(conn: Connection) -> None:
    """Both halves matter.

    A fixture identity that could not authenticate would leave the whole workflow
    untested; one that could decide real sources would put a fixture's judgement in the
    audit trail.
    """
    _, email = _enrol(conn, test_only=True)
    session = authenticate(conn, email=email, password=PASSWORD)
    assert session.is_test
    with pytest.raises(FixtureIdentityRefusedError, match="TEST ONLY"):
        refuse_test_identity_on_real_data(session)


def test_the_failure_message_does_not_distinguish_unknown_from_wrong(
    conn: Connection,
) -> None:
    """A caller that can tell them apart is an account-enumeration oracle."""
    _, email = _enrol(conn)
    with pytest.raises(AuthenticationFailedError) as unknown:
        authenticate(conn, email="nobody@example.test", password=PASSWORD)
    with pytest.raises(AuthenticationFailedError) as wrong:
        authenticate(conn, email=email, password=OTHER_PASSWORD)
    assert str(unknown.value) == str(wrong.value)


# ===========================================================================
# 12-15. The reviewer / publisher split
# ===========================================================================


def test_the_reviewer_role_does_not_carry_publication_authority(conn: Connection) -> None:
    """Section 13. Review and publication are separate authorities.

    The architecture's own separation was "you may not publish your own work", checked
    per proposal at publication time. This is the stronger form: the role that reviews
    does not hold the permission that authorises publishing at all.
    """
    held = {
        row.permission_code
        for row in conn.execute(
            text("SELECT permission_code FROM role_permission WHERE role_code = 'reviewer'")
        )
    }
    assert "proposal:publish" not in held
    assert VERIFY_PERMISSION in held
    assert "proposal:review" in held
    # Section 14: creating a proposal is a request for governed publication, not
    # publication, so the reviewer keeps it.
    assert "proposal:create" in held


def test_publication_authority_is_held_by_no_role_and_was_not_given_to_anyone(
    conn: Connection,
) -> None:
    """Section 13's explicit preference when no publisher role exists.

    Leaving it unassigned is a visible gap that somebody must decide about. Parking it
    on the reviewer would have been an invisible one.
    """
    holders = [
        row.role_code
        for row in conn.execute(
            text("SELECT role_code FROM role_permission WHERE permission_code = 'proposal:publish'")
        )
    ]
    assert holders == [], f"proposal:publish is held by {holders}"
    # The permission row itself survives, so assigning it later is one insert.
    assert (
        conn.execute(
            text("SELECT count(*) FROM permission WHERE code = 'proposal:publish'")
        ).scalar()
        == 1
    )


def test_no_reviewer_holds_publication_or_source_management(conn: Connection) -> None:
    """Section 15, on the application layer rather than the database one."""
    actor, _ = _enrol(conn)
    held = permissions_of(conn, actor)
    assert VERIFY_PERMISSION in held
    assert "proposal:publish" not in held
    assert "source:manage" not in held, "registering a source is a different authority"


def test_the_application_role_and_the_database_role_are_different_axes(
    conn: Connection, role_engines: dict[str, object]
) -> None:
    """Section 15's warning, made concrete.

    An application permission never widens a database grant. `app_publisher` is the only
    role that may write canonical tables, and no reviewer permission changes that.
    """
    for table in ("university", "tuition", "field_provenance"):
        api, publisher = conn.execute(
            text(
                "SELECT has_table_privilege('app_api', :t, 'INSERT'), "
                "       has_table_privilege('app_publisher', :t, 'INSERT')"
            ),
            {"t": table},
        ).one()
        assert not api, f"app_api can insert into {table}"
        assert publisher, f"app_publisher cannot insert into {table}"


# ===========================================================================
# 17. Granting a role
# ===========================================================================


def test_provisioning_requires_an_administrator_once_one_exists(conn: Connection) -> None:
    """Section 17. The bootstrap escape closes itself and has no re-opening flag."""
    # While no administrator exists, an unauthenticated grant is the bootstrap case.
    assert authorize_grant(conn, None) is None

    admin, _ = _enrol(conn, role="admin")
    assert GRANT_PERMISSION in permissions_of(conn, admin)

    # ...and now it is refused, permanently.
    with pytest.raises(BootstrapClosedError, match="bootstrap path closed"):
        authorize_grant(conn, None)

    # An administrator may grant; a reviewer may not. Note the `granted_by`: from here
    # on every enrolment needs one, which is the boundary doing its job.
    assert authorize_grant(conn, admin) == admin
    reviewer, _ = _enrol(conn, granted_by=admin)
    with pytest.raises(NotAuthorizedError, match=GRANT_PERMISSION):
        authorize_grant(conn, reviewer)


def test_an_authorised_grant_is_audited_and_a_bootstrap_one_is_not(
    conn: Connection,
) -> None:
    """Section 16/17: a grant with an actor is a decision; one without is not.

    Filing a bootstrap grant under SYSTEM would put a fabricated approval in the chain,
    so it is left out of it and documented instead.
    """
    before = conn.execute(text("SELECT count(*) FROM audit_log")).scalar()
    _enrol(conn)  # no administrator exists yet: bootstrap, unaudited
    assert conn.execute(text("SELECT count(*) FROM audit_log")).scalar() == before

    admin, _ = _enrol(conn, role="admin")
    unique = uuid.uuid4().hex[:8]  # granted BY the administrator below
    provision_reviewer(
        conn,
        email=f"granted-{unique}@example.test",
        display_name=f"Granted {unique}",
        test_only=True,
        granted_by=admin,
    )
    row = conn.execute(
        text(
            "SELECT action, actor_id, reason FROM audit_log "
            " WHERE action = 'ROLE_GRANTED' ORDER BY seq DESC LIMIT 1"
        )
    ).one()
    assert row.actor_id == admin
    assert "reviewer" in row.reason
