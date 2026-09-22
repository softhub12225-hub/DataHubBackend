"""Who may record a verification decision, and how a real one is provisioned.

NO INVENTED ACTOR
=================
Section 11. `provision_reviewer` takes an email and a display name and refuses to guess
either. There is no default, no generated address and no "system reviewer": an actor
exists so that a decision can be attributed to a person who made it, and one this module
made up would be a decision attributed to nobody while looking attributed.

It is also not run automatically. The operator invokes it, supplying the identity, and
the CLI says so before it writes anything.

AUTHORISATION IS CHECKED, NOT ASSUMED
=====================================
`authorize` resolves an actor to the permissions its roles carry and refuses without
`source:verify`. That permission is new in revision `d5e6f7a8b9c0` and deliberately is
**not** `source:manage`: registering a URL is data entry and declaring it official is the
act the whole C27 boundary rests on, so a `data_editor` who can add a source cannot
thereby make it publishable.

The reviewer role itself is the existing one. Section 12 forbids a second, and
`role.reviewer` already carries `is_reviewer_role = true`.

TEST IDENTITIES ARE MARKED, AND KEPT OUT OF THE REAL DATABASE
=============================================================
Section 13 permits a fixture reviewer and requires it to be obvious. `provision_reviewer`
takes `test_only`, which prefixes the display name with `[TEST ONLY]` and is the only way
this module will accept an `example.test` address. A fixture identity that looked like a
real one is how a fixture decision ends up in a real audit trail.

Step 5C.7E proved the marker alone is not enough. A script created eight properly-marked
fixture identities **in the real pilot database**, because the database was a default
nobody mentioned. So `test_only` now also refuses when the connection is attached to the
real database -- see `app.db.safety`. The check is scoped to `test_only`: provisioning a
*real* reviewer must keep working in every database, which is how Dejan exists at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, text

from app.db.safety import forbid_real_database
from app.domains.verification.audit import append as append_audit

#: The permission that authorises a verification decision. One permission, not a role.
VERIFY_PERMISSION = "source:verify"

#: The permission that authorises creating an identity or granting it a role.
GRANT_PERMISSION = "admin:roles"

#: The marker a test identity carries in its display name, so a decision made by one is
#: recognisable in the audit log without joining anything.
TEST_MARKER = "[TEST ONLY]"

#: Addresses that may only ever belong to a test identity. RFC 6761 reserves
#: `.test` for exactly this.
_TEST_DOMAINS = (".test", ".invalid", ".example")


class IdentityRefusedError(RuntimeError):
    """The identity cannot be created or used as asked. The message says why."""


class NotAuthorizedError(RuntimeError):
    """This actor may not record verification decisions."""


class BootstrapClosedError(RuntimeError):
    """An administrator exists, so provisioning may no longer run unauthenticated."""


def _administrators(connection: Connection) -> list[uuid.UUID]:
    """Every identity whose roles carry `admin:roles`, ordered for a stable message."""
    return [
        row.user_id
        for row in connection.execute(
            text(
                "SELECT DISTINCT ur.user_id FROM user_role ur "
                "  JOIN role_permission rp ON rp.role_code = ur.role_code "
                "  JOIN app_user u ON u.id = ur.user_id "
                " WHERE rp.permission_code = :perm AND u.is_active "
                " ORDER BY ur.user_id"
            ),
            {"perm": GRANT_PERMISSION},
        )
    ]


def authorize_grant(connection: Connection, granted_by: uuid.UUID | None) -> uuid.UUID | None:
    """May this actor create an identity or grant it a role? (Section 17.)

    Returns the administrator's id, or None when this is a legitimate bootstrap.

    **The bootstrap escape closes itself.** Provisioning without an administrator is
    permitted only while the database contains no active identity holding
    `admin:roles` -- that is, only when there is nobody who *could* have authorised it.
    The moment one exists, an unauthenticated grant is refused, and there is no flag to
    re-open it. A permanent escape hatch would be the same hole as an unauthenticated
    `--actor`, which is the defect this step exists to close.
    """
    existing = _administrators(connection)
    if granted_by is None:
        if existing:
            raise BootstrapClosedError(
                f"{len(existing)} administrator(s) hold {GRANT_PERMISSION!r}. "
                "Provisioning now requires one of them to authenticate; the "
                "unauthenticated bootstrap path closed when the first was created."
            )
        return None

    held = permissions_of(connection, granted_by)
    if GRANT_PERMISSION not in held:
        raise NotAuthorizedError(
            f"{granted_by} does not hold {GRANT_PERMISSION!r} and may not grant a role"
        )
    return granted_by


@dataclass(frozen=True, slots=True)
class Reviewer:
    """An authenticated identity that may record verification decisions."""

    id: uuid.UUID
    email: str
    display_name: str
    roles: tuple[str, ...]
    is_test: bool

    @property
    def actor(self) -> uuid.UUID:
        return self.id


def provision_reviewer(
    connection: Connection,
    *,
    email: str,
    display_name: str,
    role: str = "reviewer",
    test_only: bool = False,
    granted_by: uuid.UUID | None = None,
) -> Reviewer:
    """Create or re-activate an identity and give it a reviewer role.

    Refuses to invent anything. An empty email or display name is an error rather than a
    default, and a real identity may not use a reserved test domain.

    Does **not** set a password. `reviewer-set-password` does that, interactively, and
    only the person whose credential it is should run it. This establishes that a person
    exists, is who the audit trail will name, and holds the permission.

    `granted_by` is checked by `authorize_grant`: an administrator holding `admin:roles`,
    or None only while no administrator exists at all. See that function for why the
    bootstrap case closes itself.
    """
    granting_admin = authorize_grant(connection, granted_by)
    email = email.strip().lower()
    display_name = display_name.strip()
    if not email or "@" not in email:
        raise IdentityRefusedError("a reviewer needs a real email address; none was supplied")
    if not display_name:
        raise IdentityRefusedError("a reviewer needs a display name; none was supplied")

    reserved = email.endswith(_TEST_DOMAINS)
    if reserved and not test_only:
        raise IdentityRefusedError(
            f"{email} is a reserved test domain. Pass test_only to create a fixture "
            "identity, or supply a real address."
        )
    if test_only:
        # Defence in depth, and the layer that would actually have caught Step 5C.7E:
        # a script asked for a [TEST ONLY] identity while connected to the real pilot
        # database, and nothing said no. A fixture identity there is how a fixture
        # decision reaches a real audit trail. Scoped to `test_only` so that
        # provisioning a real reviewer keeps working in every database, including this
        # one -- which is how Dejan was created.
        forbid_real_database(connection, context=f"provision_reviewer({email})")
    if test_only and not reserved:
        raise IdentityRefusedError(
            "a test identity must use a reserved domain (.test, .invalid, .example) so "
            "it cannot be mistaken for a real reviewer"
        )
    if test_only and not display_name.startswith(TEST_MARKER):
        display_name = f"{TEST_MARKER} {display_name}"

    if not connection.execute(
        text("SELECT 1 FROM role WHERE code = :code"), {"code": role}
    ).one_or_none():
        raise IdentityRefusedError(f"no role {role!r}")

    row = connection.execute(
        text("SELECT id, display_name FROM app_user WHERE email = :email FOR UPDATE"),
        {"email": email},
    ).one_or_none()
    if row is None:
        actor_id = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO app_user (id, email, display_name, is_active) "
                "VALUES (:id, :email, :name, true)"
            ),
            {"id": actor_id, "email": email, "name": display_name},
        )
    else:
        actor_id = row.id
        connection.execute(
            text(
                "UPDATE app_user SET is_active = true, deactivated_at = NULL, "
                "display_name = :name, updated_at = now() WHERE id = :id"
            ),
            {"id": actor_id, "name": display_name},
        )

    connection.execute(
        text(
            "INSERT INTO user_role (id, user_id, role_code, granted_by) "
            "VALUES (:id, :user, :role, :by) ON CONFLICT DO NOTHING"
        ),
        {"id": uuid.uuid4(), "user": actor_id, "role": role, "by": granting_admin},
    )
    if granting_admin is not None:
        # Only an authorised grant is auditable as a decision. A bootstrap grant has no
        # actor to name, and inventing one -- or filing it under SYSTEM as though a
        # machine had decided it -- would put a fabricated approval in the chain.
        append_audit(
            connection,
            actor_id=granting_admin,
            action="ROLE_GRANTED",
            object_type="app_user",
            object_id=actor_id,
            reason=f"granted {role!r} to {email}",
            after={"role": role, "email": email},
        )
    return describe(connection, actor_id)


def describe(connection: Connection, actor_id: uuid.UUID) -> Reviewer:
    """Read an identity back, with its roles. Raises if it does not exist."""
    row = connection.execute(
        text("SELECT id, email, display_name, is_active FROM app_user WHERE id = :id"),
        {"id": actor_id},
    ).one_or_none()
    if row is None:
        raise IdentityRefusedError(f"no app_user {actor_id}")
    roles = tuple(
        entry.role_code
        for entry in connection.execute(
            text("SELECT role_code FROM user_role WHERE user_id = :id ORDER BY role_code"),
            {"id": actor_id},
        )
    )
    return Reviewer(
        id=row.id,
        email=str(row.email),
        display_name=str(row.display_name),
        roles=roles,
        is_test=str(row.display_name).startswith(TEST_MARKER),
    )


def permissions_of(connection: Connection, actor_id: uuid.UUID) -> frozenset[str]:
    """Every permission this actor's roles carry."""
    return frozenset(
        row.permission_code
        for row in connection.execute(
            text(
                "SELECT rp.permission_code FROM user_role ur "
                "  JOIN role_permission rp ON rp.role_code = ur.role_code "
                " WHERE ur.user_id = :id"
            ),
            {"id": actor_id},
        )
    )


def authorize(connection: Connection, actor_id: uuid.UUID | None) -> Reviewer:
    """Resolve an actor and refuse unless it may verify sources.

    A missing actor is refused first and by name: "no actor" and "wrong actor" are
    different mistakes and a caller should be able to tell them apart.
    """
    if actor_id is None:
        raise NotAuthorizedError("a verification decision requires an actor; none was supplied")
    reviewer = describe(connection, actor_id)
    active = connection.execute(
        text("SELECT is_active FROM app_user WHERE id = :id"), {"id": actor_id}
    ).scalar()
    if not active:
        raise NotAuthorizedError(f"{reviewer.email} is deactivated")
    if VERIFY_PERMISSION not in permissions_of(connection, actor_id):
        raise NotAuthorizedError(
            f"{reviewer.email} holds {sorted(reviewer.roles) or 'no roles'} and none of "
            f"them carries {VERIFY_PERMISSION!r}"
        )
    return reviewer


__all__ = [
    "GRANT_PERMISSION",
    "TEST_MARKER",
    "VERIFY_PERMISSION",
    "BootstrapClosedError",
    "IdentityRefusedError",
    "NotAuthorizedError",
    "Reviewer",
    "authorize",
    "authorize_grant",
    "describe",
    "permissions_of",
    "provision_reviewer",
]
