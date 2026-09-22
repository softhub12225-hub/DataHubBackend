"""Fetch-time network safety. The real SSRF defence (Step 5B section 10-12).

WHY THIS EXISTS SEPARATELY FROM `onboarding/urls.py`
====================================================
That module validates a URL when it is **stored**: scheme, authority shape, port, no
IP literal, no credentials, no obviously-internal name. It performs no I/O and says so
loudly, because a DNS answer obtained at registration time says nothing about the
answer obtained when the crawler runs.

This module runs at **fetch** time, where the actual attack surface is:

* a hostname that resolves to a private address -- today, or only when it matters;
* a redirect from a public page to `http://127.0.0.1:6379`;
* a cloud metadata endpoint, which is the one that leaks credentials rather than
  merely reaching something internal;
* numeric host spellings that look like nothing (`127.1`, `0177.0.0.1`,
  `2130706433`), which is how a blocklist of strings gets bypassed.

Storage validation is the floor. This is the defence.

WHAT IS CHECKED, AND AGAINST WHAT
=================================
Every A and AAAA answer, not just the first. A name resolving to one public and one
private address is a rebinding attempt wearing a hat, and accepting it because the
first answer looked fine is the whole bug.

The address classification is deliberately allow-by-exclusion over the whole IP space
rather than a blocklist of ranges someone remembered: `ipaddress` already knows which
addresses are private, loopback, link-local, multicast, reserved or unspecified, and
using its predicates means a range added to a future RFC does not need a code change
here. Cloud metadata addresses are then named explicitly on top, because
`169.254.169.254` is link-local (already refused) but `100.100.100.200` is not.

DNS REBINDING: WHAT WE DO, AND THE LIMIT
========================================
The TOCTOU hole is real and cannot be fully closed at this layer. Between validating a
resolution and opening a socket, a hostile resolver can answer differently.

What this module does about it:

1. Resolve once, validate **every** answer, and keep the vetted addresses.
2. Connect to a **pinned** address from that set, not to the hostname. `PinnedTransport`
   rewrites the connection target while leaving `Host:` and the TLS SNI as the original
   name, so the socket goes where we checked and the server still serves the right
   virtual host and certificate.
3. Re-run all of it on every redirect hop, against the hop's own host.

**The limit, stated rather than hidden:** pinning closes the gap between *our*
resolution and *our* connection. It does not defend against a name whose legitimate
answer is itself hostile, nor against an attacker who controls DNS and returns a
public address that they also control and which proxies inward. Those are not DNS
rebinding; they are "the host is malicious", which no client-side check can detect.

A second limit: this validates at connect time for the addresses we resolved. If the
platform's resolver is bypassed (a `/etc/hosts` entry, a hostile local resolver),
`getaddrinfo` returns what it is told and we validate what we are given. Controlling
the resolver is part of deploying this system, not something the client can assert.

Nothing here is relaxed because a URL came from the client's workbook. A spreadsheet
is not a trust boundary.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.logging import get_logger
from app.domains.onboarding.urls import ALLOWED_PORTS, ALLOWED_SCHEMES, UrlRejectedError

logger = get_logger(__name__)

#: Cloud metadata services. `169.254.169.254` is link-local and already refused by the
#: range check; it is listed anyway so the *reason* in the log says "metadata endpoint"
#: rather than "link-local", which is the difference between an operator understanding
#: the alert and filing it. The others are not in any otherwise-refused range:
#: Alibaba and Oracle both use ordinary public-looking addresses.
METADATA_ADDRESSES: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS, Azure, GCP, DigitalOcean, OpenStack
        "169.254.170.2",  # AWS ECS task metadata
        "fd00:ec2::254",  # AWS IMDSv6
        "100.100.100.200",  # Alibaba Cloud
        "192.0.0.192",  # Oracle Cloud
    }
)

#: Hostnames that name a metadata service by DNS. Refused before resolution, because
#: resolving them is itself a signal and the answer may be a public-looking address.
METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

#: Longest redirect chain followed. Beyond this a page is either misconfigured or
#: playing a game, and both are worth surfacing rather than following.
MAX_REDIRECTS = 5


class TargetRefusedError(RuntimeError):
    """Base: we did not connect. **Why** decides what happens next, so subclasses.

    Collapsing these into one exception is the defect Step 5B.2 removes. A resolver
    timeout and *"this hostname resolves to 127.0.0.1"* arrived here as the same
    error, so both produced a permanent security block -- and a DNS hiccup during a
    319-page run silently dropped a legitimate page for good.
    """


class UnsafeTargetError(TargetRefusedError):
    """The target may not be contacted, as a matter of security.

    Non-global resolved address, IP literal, bad scheme, credentials in the URL, a
    cloud metadata endpoint. **Never retried automatically**: the answer will be the
    same, and a system that keeps trying an address it has refused is one bad DNS
    answer away from doing what the refusal existed to prevent.
    """


class DnsTemporaryError(TargetRefusedError):
    """The resolver did not answer, and might next time.

    Timeout, `SERVFAIL`, `EAI_AGAIN`. This says nothing about the target -- we never
    learned anything about it -- so it earns a bounded backoff and no judgement.
    """


class NameNotResolvedError(TargetRefusedError):
    """The name does not exist (`NXDOMAIN`).

    Distinct from a temporary failure because retrying cannot help: no number of
    lookups invents a hostname. A person has to look at the URL, which is what
    `NEEDS_MANUAL_REVIEW` is for.
    """


#: `getaddrinfo` failure codes that mean "ask again later" rather than "no such name".
#:
#: Both families are listed, by attribute **and** by literal, because the constants
#: differ by platform and each platform is missing the other's: Windows has no
#: `socket.EAI_AGAIN`, glibc has no `WSATRY_AGAIN`. Attribute lookups get the running
#: platform right; the literals make the classifier answer the same way for a code
#: from either, which is what lets one test suite cover both. glibc's values are
#: negative and Winsock's are in the 11000s, so the two cannot collide.
_TEMPORARY_DNS_CODES: frozenset[int] = frozenset(
    code
    for code in (
        getattr(socket, "EAI_AGAIN", None),
        getattr(socket, "EAI_FAIL", None),
        getattr(socket, "EAI_SYSTEM", None),
        -3,  # EAI_AGAIN   (glibc) -- resolver said "try again"
        -4,  # EAI_FAIL    (glibc) -- SERVFAIL; the spec calls this temporary
        -11,  # EAI_SYSTEM (glibc)
        11002,  # WSATRY_AGAIN
        11003,  # WSANO_RECOVERY
    )
    if isinstance(code, int)
)

#: Codes that mean the name is simply not there.
_NOT_FOUND_DNS_CODES: frozenset[int] = frozenset(
    code
    for code in (
        getattr(socket, "EAI_NONAME", None),
        getattr(socket, "EAI_NODATA", None),
        -2,  # EAI_NONAME (glibc) -- NXDOMAIN
        -5,  # EAI_NODATA (glibc)
        11001,  # WSAHOST_NOT_FOUND -- NXDOMAIN
        11004,  # WSANO_DATA
    )
    if isinstance(code, int)
)


def classify_resolver_failure(exc: OSError) -> TargetRefusedError:
    """Turn a `getaddrinfo` failure into the error whose consequence is correct.

    Unknown codes are treated as **temporary**, deliberately. Getting it wrong in that
    direction costs one wasted retry; getting it wrong the other way parks a working
    page in `NEEDS_MANUAL_REVIEW` and waits for a human who has no reason to look.
    """
    code = getattr(exc, "errno", None)
    host_detail = str(exc) or type(exc).__name__
    if isinstance(code, int) and code in _NOT_FOUND_DNS_CODES:
        return NameNotResolvedError(f"the name does not resolve: {host_detail}")
    if isinstance(code, int) and code in _TEMPORARY_DNS_CODES:
        return DnsTemporaryError(f"the resolver did not answer: {host_detail}")
    return DnsTemporaryError(f"DNS lookup failed ({code}): {host_detail}")


@dataclass(frozen=True, slots=True)
class VettedTarget:
    """A host that resolved entirely to public addresses, and where to connect."""

    host: str
    port: int
    scheme: str
    #: Every answer, all of them validated. Connecting to any is safe; the fetcher
    #: takes the first and may fall back through the rest.
    addresses: tuple[str, ...]

    @property
    def primary(self) -> str:
        return self.addresses[0]


def classify_address(raw: str) -> str | None:
    """Return why an address is unsafe, or None if it is a public unicast address.

    Exclusion over the whole space rather than a remembered blocklist: `ipaddress`
    already knows these categories, so a range added to a future RFC is handled
    without a change here.
    """
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return f"not an IP address: {raw!r}"

    if raw in METADATA_ADDRESSES or str(address) in METADATA_ADDRESSES:
        return f"cloud metadata endpoint ({address})"

    if isinstance(address, ipaddress.IPv6Address):
        # A v4-mapped or 6to4 v6 address carries a v4 address inside it, and the v6
        # predicates above do not look at it. `::ffff:127.0.0.1` is not "loopback" to
        # `ipaddress`, but it reaches loopback.
        if address.ipv4_mapped is not None:
            inner = classify_address(str(address.ipv4_mapped))
            return None if inner is None else f"IPv4-mapped {inner}"
        if address.sixtofour is not None:
            inner = classify_address(str(address.sixtofour))
            return None if inner is None else f"6to4-embedded {inner}"
        if address.teredo is not None:
            inner = classify_address(str(address.teredo[1]))
            return None if inner is None else f"Teredo-embedded {inner}"

    # `is_global` is the primary test, and the specific predicates below only
    # produce a readable reason. Using `is_private` as the gate would have let
    # 100.64.0.0/10 through on this Python: RFC 6598 shared address space is not
    # "private" to `ipaddress` and is not globally routable either, and an address a
    # carrier NATs is exactly the kind that reaches something we did not intend.
    if address.is_global:
        return None

    if address.is_unspecified:
        return f"unspecified address ({address})"
    if address.is_loopback:
        return f"loopback address ({address})"
    if address.is_link_local:
        return f"link-local address ({address})"
    if address.is_multicast:
        return f"multicast address ({address})"
    if address.is_reserved:
        return f"reserved address ({address})"
    if address.is_private:
        return f"private address ({address})"
    return f"not a globally routable address ({address})"


def check_url_shape(url: str) -> tuple[str, str, int]:
    """Scheme, host and port of a URL we are willing to *attempt*. No I/O.

    Deliberately re-checks what storage validation already checked. This is called on
    every redirect target, and a redirect target has been through no storage
    validation at all -- it arrived in a `Location` header from the open internet.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeTargetError(f"scheme {scheme or '(none)'!r} is not http or https")
    if parts.username or parts.password:
        raise UnsafeTargetError("URL carries credentials in the authority")

    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise UnsafeTargetError("URL has no host")
    if host in METADATA_HOSTS:
        raise UnsafeTargetError(f"host {host!r} names a cloud metadata service")

    # An IP literal never appears in a legitimate university URL and is the shortest
    # path to a private address. `ipaddress` parses the numeric spellings that string
    # blocklists miss -- `2130706433`, `0177.0.0.1`, `127.1` -- so the refusal covers
    # them without enumerating them (Step 4 fixes, preserved).
    literal = _as_ip_literal(host)
    if literal is not None:
        reason = classify_address(literal)
        raise UnsafeTargetError(
            f"host is an IP literal ({host!r} -> {literal})"
            + (f": {reason}" if reason else "; only DNS names may be fetched")
        )

    try:
        port = parts.port
    except ValueError as exc:  # non-numeric port
        raise UnsafeTargetError(f"invalid port in URL: {exc}") from exc
    resolved_port = port if port is not None else (443 if scheme == "https" else 80)
    if resolved_port not in ALLOWED_PORTS:
        raise UnsafeTargetError(f"port {resolved_port} is not a normal web port")

    return scheme, host, resolved_port


def _as_ip_literal(host: str) -> str | None:
    """The address a host string denotes, if it denotes one at all.

    `ipaddress.ip_address` rejects the shortened and octal forms, which is exactly
    why they are useful to an attacker. `socket.inet_aton` accepts them, so it is
    consulted second -- `127.1` becomes `127.0.0.1` and is then classified normally.
    """
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    # Only try the loose form on something that is entirely digits, dots and the
    # 0x/0 prefixes. `inet_aton` would otherwise resolve nothing but still cost a
    # syscall on every ordinary hostname.
    if not all(character in "0123456789abcdefABCDEFxX." for character in candidate):
        return None
    try:
        packed = socket.inet_aton(candidate)
    except OSError:
        return None
    return str(ipaddress.IPv4Address(packed))


async def resolve_and_validate(url: str, *, timeout_seconds: float = 5.0) -> VettedTarget:
    """Resolve a URL's host and refuse it unless **every** answer is public.

    Every answer, because a name resolving to one public and one private address is
    a rebinding attempt, and validating only the first is the bug this is written to
    avoid. The whole set is returned so the fetcher can pin to one of them.
    """
    scheme, host, port = check_url_shape(url)

    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        # Our own deadline, not the resolver's answer: nothing was learned about the
        # target, so this is timing and not a security finding.
        raise DnsTemporaryError(f"DNS lookup for {host!r} timed out") from exc
    except OSError as exc:
        raise classify_resolver_failure(exc) from exc

    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        # A successful lookup that returned nothing is the name having no address --
        # a URL problem for a person, not a security refusal and not worth retrying.
        raise NameNotResolvedError(f"{host!r} resolved to no addresses")

    for address in addresses:
        reason = classify_address(address)
        if reason is not None:
            logger.warning("acquisition_target_refused", host=host, address=address, reason=reason)
            raise UnsafeTargetError(f"{host!r} resolves to a {reason}")

    return VettedTarget(host=host, port=port, scheme=scheme, addresses=tuple(addresses))


def validate_stored_url_for_fetch(url: str) -> None:
    """Shape-only pre-flight, for deciding `fetch_eligibility` without touching DNS.

    Separate from `resolve_and_validate` because eligibility is a property of the
    source, computed when it is registered, while resolution is a property of the
    moment and must happen again at every fetch. Conflating them would either cache a
    DNS answer into a database column or make registration do network I/O.
    """
    try:
        check_url_shape(url)
    except UnsafeTargetError:
        raise
    except UrlRejectedError as exc:  # pragma: no cover - defensive
        raise UnsafeTargetError(str(exc)) from exc


__all__ = [
    "MAX_REDIRECTS",
    "METADATA_ADDRESSES",
    "METADATA_HOSTS",
    "DnsTemporaryError",
    "NameNotResolvedError",
    "TargetRefusedError",
    "UnsafeTargetError",
    "VettedTarget",
    "check_url_shape",
    "classify_address",
    "classify_resolver_failure",
    "resolve_and_validate",
    "validate_stored_url_for_fetch",
]
