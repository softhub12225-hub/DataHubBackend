"""Where the bytes actually came from, and whether that host is trusted too.

THE HOLE THIS CLOSES
====================
``register_verified_candidate`` and ``promote`` both reasoned about a single host: the
one in the submitted URL. Neither looked at where the fetch actually landed. Three of the
six ANU mappings redirect::

    https://www.anu.edu.au/study/apply   -- 301 -->   https://study.anu.edu.au/apply

The mapping hangs off ``www.anu.edu.au``'s ``official_domain`` row and is promoted on that
row's verification. The body that will be extracted, quoted and published came from
``study.anu.edu.au``. Nothing compared the two.

Here that is harmless, and only by luck: Dejan independently verified all three ANU hosts,
so the effective host carries its own authority. Change one fact -- a 301 to a host nobody
verified, a marketing subdomain, a third-party application portal -- and a source would
have been promoted on the authority of a host that served none of its content. That is
precisely the substitution the whole trust chain exists to prevent.

WHAT "TRUSTED" MEANS HERE
=========================
The same thing it means everywhere else in the chain, asked of the effective host:

* an ``official_domain`` row exists for it,
* its ``verification_status`` is ``VERIFIED_OFFICIAL`` or ``AUTHORIZED_EXTERNAL``,
* it is ``is_active``,
* and it is bound to the **same institution** as the requested host.

The last clause is not redundant. Two separately verified hosts belonging to two different
institutions must not launder authority across the boundary between them: ANU's
verification of ``study.anu.edu.au`` says nothing about a redirect into another
university's domain, even a verified one.

NO REDIRECT IS NOT A PASS BY DEFAULT
====================================
When the effective host equals the requested host there is only one host to judge, and it
still has to be trusted. When no snapshot exists at all the answer is ``NO_EVIDENCE``, not
``OK``: a source with no stored evidence has not demonstrated where its bytes come from,
and promotion should refuse rather than assume the happy case.

A NULL ``effective_url`` IS NOT "NO EVIDENCE"
=============================================
The recorder writes ``snapshot.effective_url`` only when the fetch actually moved. S0232
(``www.anu.edu.au/``) and S0236 (``programsandcourses.anu.edu.au/``) each hold a 200
snapshot with ``effective_url`` NULL, because neither redirected. Treating that as missing
evidence would have blocked promotion of two perfectly sound sources -- the first draft of
this module did exactly that. The distinction that matters is *is there a snapshot row*,
not *is there a redirect*:

* no snapshot row            -> ``NO_EVIDENCE``
* snapshot, NULL effective   -> no redirect happened; judge the requested host alone
* snapshot, effective set    -> compare the two hosts
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from sqlalchemy import Connection, text

#: Verification states under which a host may carry content into the publishable chain.
#: Identical to the set `promote` already applies to the requested host's domain.
TRUSTED_STATUSES = frozenset({"VERIFIED_OFFICIAL", "AUTHORIZED_EXTERNAL"})


class AuthorityVerdict(StrEnum):
    """Whether the hosts involved in this fetch may carry it into the trust chain."""

    OK = "OK"
    """Every host that served content is trusted, active and one institution's."""

    NO_EVIDENCE = "NO_EVIDENCE"
    """No snapshot, so there is no record of where the bytes came from."""

    REQUESTED_UNTRUSTED = "REQUESTED_UNTRUSTED"
    """The submitted host is not (or is no longer) trusted."""

    EFFECTIVE_UNTRUSTED = "EFFECTIVE_UNTRUSTED"
    """The redirect target is not trusted. The substitution this module exists for."""

    INSTITUTION_MISMATCH = "INSTITUTION_MISMATCH"
    """Both hosts are trusted, and they belong to different institutions."""


@dataclass(frozen=True, slots=True)
class HostAuthority:
    """One host, and whether the database currently trusts it."""

    host: str
    official_domain_id: uuid.UUID | None
    verification_status: str | None
    is_active: bool | None
    institution_id: uuid.UUID | None

    @property
    def trusted(self) -> bool:
        return (
            self.official_domain_id is not None
            and self.verification_status in TRUSTED_STATUSES
            and bool(self.is_active)
        )

    @property
    def display(self) -> str:
        """A short phrase for the console. Never invents a state it does not have."""
        if self.official_domain_id is None:
            return "NOT REGISTERED"
        state = self.verification_status or "UNKNOWN"
        return state if self.is_active else f"{state} (INACTIVE)"


@dataclass(frozen=True, slots=True)
class RedirectAuthority:
    """The requested host, the host that answered, and the verdict on the pair."""

    requested: HostAuthority
    effective: HostAuthority
    redirected: bool
    effective_url: str | None
    redirect_chain: list[dict[str, object]]
    verdict: AuthorityVerdict

    @property
    def ok(self) -> bool:
        return self.verdict is AuthorityVerdict.OK

    @property
    def blocker(self) -> str | None:
        """Why promotion must refuse, phrased for a reviewer. ``None`` when it may proceed."""
        if self.verdict is AuthorityVerdict.OK:
            return None
        if self.verdict is AuthorityVerdict.NO_EVIDENCE:
            return (
                "no stored snapshot, so there is no record of which host actually served "
                "this page. Acquire evidence before promoting it."
            )
        if self.verdict is AuthorityVerdict.REQUESTED_UNTRUSTED:
            return (
                f"the submitted host {self.requested.host} is "
                f"{self.requested.display}; verify the host first."
            )
        if self.verdict is AuthorityVerdict.EFFECTIVE_UNTRUSTED:
            return (
                f"{self.requested.host} redirects to {self.effective.host}, which is "
                f"{self.effective.display}. The content comes from the redirect target, "
                "so promoting on the submitted host's authority would attach a trusted "
                "name to an untrusted page."
            )
        return (
            f"{self.requested.host} belongs to institution "
            f"{self.requested.institution_id} and {self.effective.host} to "
            f"{self.effective.institution_id}. Authority does not cross institutions."
        )


def _authority(connection: Connection, host: str) -> HostAuthority:
    """What the database currently says about one host. Read-only."""
    row = connection.execute(
        text(
            """
            SELECT id, verification_status::text AS status, is_active,
                   target_institution_id
              FROM official_domain
             WHERE host = :host
            """
        ),
        {"host": host.strip().lower()},
    ).one_or_none()
    if row is None:
        return HostAuthority(
            host=host,
            official_domain_id=None,
            verification_status=None,
            is_active=None,
            institution_id=None,
        )
    return HostAuthority(
        host=host,
        official_domain_id=uuid.UUID(str(row.id)),
        verification_status=str(row.status),
        is_active=bool(row.is_active),
        institution_id=(
            uuid.UUID(str(row.target_institution_id))
            if row.target_institution_id is not None
            else None
        ),
    )


def for_mapping(connection: Connection, mapping_id: uuid.UUID) -> RedirectAuthority:
    """Resolve redirect authority for one ``source_mapping``.

    The effective URL is read from the most recent stored snapshot of the acquisition
    source behind the mapping's workbook row. It is never refetched: this answers *where
    did the evidence we hold come from*, and a fresh fetch would answer a different
    question about a page that may since have changed.
    """
    row = connection.execute(
        text(
            """
            SELECT sm.host AS requested_host,
                   pcs.acquisition_source_id AS acquisition_source_id
              FROM source_mapping sm
              LEFT JOIN pilot_collected_source pcs
                     ON pcs.promoted_source_mapping_id = sm.id
             WHERE sm.id = :mapping
            """
        ),
        {"mapping": mapping_id},
    ).one_or_none()
    if row is None:
        raise LookupError(f"no source_mapping {mapping_id}")
    return _resolve(
        connection,
        requested_host=str(row.requested_host),
        acquisition_source_id=row.acquisition_source_id,
    )


def for_pilot_source(connection: Connection, pilot_source_id: uuid.UUID) -> RedirectAuthority:
    """Resolve redirect authority for one workbook row, registered or not."""
    row = connection.execute(
        text(
            """
            SELECT host AS requested_host, acquisition_source_id
              FROM pilot_collected_source
             WHERE id = :pilot
            """
        ),
        {"pilot": pilot_source_id},
    ).one_or_none()
    if row is None:
        raise LookupError(f"no pilot_collected_source {pilot_source_id}")
    return _resolve(
        connection,
        requested_host=str(row.requested_host),
        acquisition_source_id=row.acquisition_source_id,
    )


def _resolve(
    connection: Connection,
    *,
    requested_host: str,
    acquisition_source_id: uuid.UUID | None,
) -> RedirectAuthority:
    """The shared body. Kept private so the two entry points cannot drift apart."""
    requested = _authority(connection, requested_host)

    snapshot = None
    if acquisition_source_id is not None:
        snapshot = connection.execute(
            text(
                """
                SELECT effective_url, redirect_chain
                  FROM snapshot
                 WHERE source_id = :source
                 ORDER BY observed_at DESC
                 LIMIT 1
                """
            ),
            {"source": acquisition_source_id},
        ).one_or_none()

    if snapshot is None:
        # Genuinely nothing stored. Not the same as "did not redirect".
        return RedirectAuthority(
            requested=requested,
            effective=requested,
            redirected=False,
            effective_url=None,
            redirect_chain=[],
            verdict=AuthorityVerdict.NO_EVIDENCE,
        )

    # A NULL effective_url means the fetch did not move, so the requested host is the
    # only host that served anything. See the module docstring.
    effective_host = (
        (urlsplit(str(snapshot.effective_url)).hostname or "").lower()
        if snapshot.effective_url
        else requested_host.strip().lower()
    )
    redirected = bool(effective_host) and effective_host != requested_host.strip().lower()
    effective = _authority(connection, effective_host) if redirected else requested
    chain = list(snapshot.redirect_chain or [])

    verdict = AuthorityVerdict.OK
    if not requested.trusted:
        verdict = AuthorityVerdict.REQUESTED_UNTRUSTED
    elif redirected and not effective.trusted:
        verdict = AuthorityVerdict.EFFECTIVE_UNTRUSTED
    elif redirected and requested.institution_id != effective.institution_id:
        verdict = AuthorityVerdict.INSTITUTION_MISMATCH

    return RedirectAuthority(
        requested=requested,
        effective=effective,
        redirected=redirected,
        effective_url=(str(snapshot.effective_url) if snapshot.effective_url else None),
        redirect_chain=chain,
        verdict=verdict,
    )


__all__ = [
    "TRUSTED_STATUSES",
    "AuthorityVerdict",
    "HostAuthority",
    "RedirectAuthority",
    "for_mapping",
    "for_pilot_source",
]
