"""Registration-time URL validation and normalisation.

SCOPE OF THIS MODULE -- READ BEFORE RELYING ON IT
=================================================
This is the guard applied when a URL is **stored**. It is a necessary filter and an
insufficient one, and treating it as sufficient would be the mistake it exists to
prevent.

What it establishes, at registration time:

* the scheme is ``http`` or ``https`` and nothing else;
* the authority is a DNS name, not an IP literal, not a ``userinfo`` credential,
  and not a name that is obviously internal;
* the port, if given, is a normal web port;
* the string round-trips to a canonical form, so the same page is not registered
  twice under two spellings.

What it CANNOT establish, ever:

* that the name resolves to a public address. DNS is not consulted here, and must
  not be: a resolution performed now says nothing about the resolution performed at
  fetch time. ``evil.example.com`` may answer ``93.184.216.34`` today and
  ``169.254.169.254`` when the crawler runs -- DNS rebinding, which no amount of
  string validation detects.
* that a redirect chain stays public. A permitted URL may 302 to
  ``http://127.0.0.1:6379``.

**Therefore the acquisition phase must apply its own SSRF controls at fetch time**:
resolve the hostname, reject non-public addresses across *every* A/AAAA answer,
pin the connection to the vetted address, re-run the check on every redirect hop,
and cap the hop count. Those controls are the real defence. This module only ensures
that a URL which was never allowed to be stored cannot arrive at them at all --
importing a spreadsheet, or pasting a link into the console, is not a way around
them (Step 4 requirement 15).

Nothing here performs I/O. No DNS lookup, no connection, no fetch.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit, urlunsplit

#: The only schemes that may be stored. `source` URLs are pages and documents to be
#: retrieved over the web; every other scheme either cannot be fetched or names a
#: local resource.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Ports a university publishes on. An arbitrary high port on an official host is
#: more likely a service than a page, and allowing it widens what a stored URL can
#: reach if the fetch-time guard is ever weakened.
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})

_DEFAULT_PORTS = {"http": 80, "https": 443}

#: Hostnames and suffixes that never denote a public university site. Matched on
#: the whole name or on a dot-suffix, never as a substring: ``localhost`` must not
#: reject ``localhost-college.ac.uk``.
BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "metadata"})
BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".corp",
    ".home",
    ".lan",
    ".test",
    ".example",
    ".invalid",
    ".onion",
)

#: A DNS name: labels of alphanumerics and hyphens, at least two labels, no label
#: starting or ending with a hyphen. Applied after IDNA encoding, so a
#: internationalised name is checked in its ASCII form.
_HOSTNAME = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?" r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)

#: Characters that must not appear anywhere in a supplied URL. Whitespace and
#: control characters enable request smuggling and header injection downstream, and
#: a URL containing them is malformed regardless.
_FORBIDDEN_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")

#: Longest URL accepted. Well past any real page, short of the strings that exist to
#: overflow something.
MAX_URL_LENGTH = 2048


class UrlRejectedError(ValueError):
    """A URL may not be stored. The message names the reason, for the operator."""


@dataclass(frozen=True, slots=True)
class NormalizedUrl:
    """A URL accepted for storage, with the derived columns it populates."""

    original: str
    normalized: str
    host: str
    scheme: str
    port: int | None
    sha256: str


def _idna_host(host: str) -> str:
    """Lowercase and IDNA-encode a hostname.

    Done before pattern matching so an internationalised domain is validated as the
    ASCII name that will actually be resolved, rather than as its Unicode display
    form. This also neutralises homograph spellings: two visually identical names
    encode to two different, distinguishable ``xn--`` labels.
    """
    host = host.strip().rstrip(".").lower()
    if not host:
        raise UrlRejectedError("URL has no host")
    try:
        # encode() rejects labels that are too long or structurally invalid.
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UrlRejectedError(f"host is not a valid international domain name: {host!r}") from exc


def validate_source_url(raw: str) -> NormalizedUrl:
    """Validate and canonicalise a URL for storage, or raise `UrlRejectedError`.

    Canonicalisation is limited to changes that cannot alter which resource is
    addressed: the scheme and host are lowercased, a default port is dropped, an
    empty path becomes ``/``, and the fragment is discarded because a server never
    sees it. The query string is preserved untouched -- many official pages are
    query-addressed, and "tidying" it would fetch a different page.
    """
    if not isinstance(raw, str):
        raise UrlRejectedError(f"expected a string URL, got {type(raw).__name__}")

    candidate = raw.strip()
    if not candidate:
        raise UrlRejectedError("URL is empty")
    if len(candidate) > MAX_URL_LENGTH:
        raise UrlRejectedError(f"URL exceeds {MAX_URL_LENGTH} characters")
    if _FORBIDDEN_CHARS.search(candidate):
        raise UrlRejectedError("URL contains whitespace or control characters")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise UrlRejectedError(f"URL is unparseable: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlRejectedError(
            f"scheme {parts.scheme!r} is not permitted; only "
            f"{', '.join(sorted(ALLOWED_SCHEMES))} may be stored"
        )

    # Credentials in a URL are never part of a public page's address, and a stored
    # `user:pass@` would be a leaked secret as well as an SSRF vector.
    if parts.username is not None or parts.password is not None:
        raise UrlRejectedError("URL must not contain embedded credentials")

    if "@" in parts.netloc:
        raise UrlRejectedError("URL authority must not contain '@'")

    try:
        raw_host = parts.hostname
    except ValueError as exc:
        raise UrlRejectedError(f"URL authority is malformed: {exc}") from exc
    if not raw_host:
        raise UrlRejectedError("URL has no host")

    # An IP literal cannot be shown to belong to an institution by any of the
    # recorded verification methods, and bracketed IPv6 is the usual way a loopback
    # or link-local address is smuggled past a naive check.
    if parts.netloc.startswith("["):
        raise UrlRejectedError("IPv6 literals are not permitted; register a hostname")
    try:
        ipaddress.ip_address(raw_host)
    except ValueError:
        pass
    else:
        raise UrlRejectedError(
            f"IP literals are not permitted; register a hostname (got {raw_host})"
        )

    host = _idna_host(raw_host)
    if not _HOSTNAME.match(host):
        raise UrlRejectedError(f"host is not a valid public DNS name: {host!r}")

    # `ipaddress.ip_address` is strict and rejects the abbreviated, octal and decimal
    # spellings of an address -- `127.1`, `0177.0.0.1`, `2130706433` -- but
    # `inet_aton`, and therefore the OS resolver and most HTTP clients, accept them as
    # 127.0.0.1. They pass the DNS-name pattern above because their labels are
    # alphanumeric, which makes them a well-worn way past a naive check.
    #
    # The rule that catches all of them at once: a real public suffix always contains
    # a non-digit. No delegated TLD is numeric, and RFC 3696 prohibits one, so this
    # has no false positives on genuine hostnames.
    if host.rsplit(".", 1)[-1].isdigit():
        raise UrlRejectedError(
            f"host {host!r} is a numeric address form, not a hostname; a public "
            "suffix is never all digits"
        )
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
        raise UrlRejectedError(f"host {host!r} is a local or reserved name")

    try:
        port = parts.port
    except ValueError as exc:
        raise UrlRejectedError(f"URL port is invalid: {exc}") from exc
    if port is not None and port not in ALLOWED_PORTS:
        raise UrlRejectedError(
            f"port {port} is not permitted; allowed ports are "
            f"{', '.join(str(p) for p in sorted(ALLOWED_PORTS))}"
        )

    # Drop the port when it is the scheme's default, so http://x and http://x:80 do
    # not register as two sources.
    explicit_port = None if port is None or port == _DEFAULT_PORTS[scheme] else port
    netloc = host if explicit_port is None else f"{host}:{explicit_port}"

    path = _normalize_path(parts.path)
    normalized = urlunsplit((scheme, netloc, path, parts.query, ""))

    return NormalizedUrl(
        original=candidate,
        normalized=normalized,
        host=host,
        scheme=scheme,
        port=explicit_port,
        sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


def _normalize_path(path: str) -> str:
    """Canonicalise a path without changing which resource it addresses.

    Percent-encoding is re-applied consistently so ``/a%2Fb`` and ``/a/b`` stay
    distinct while ``/A%20B`` and ``/A B`` converge. Dot segments are *not*
    collapsed: on a fair number of university sites ``/a/../b`` and ``/b`` are
    served by different handlers, and rewriting the path would silently register a
    URL nobody chose.
    """
    if not path:
        return "/"
    # safe= keeps the reserved delimiters that carry meaning in a path, so only
    # genuinely unsafe octets are encoded.
    return quote(unquote(path), safe="/-._~!$&'()*+,;=:@%")


__all__ = [
    "ALLOWED_PORTS",
    "ALLOWED_SCHEMES",
    "BLOCKED_HOSTS",
    "BLOCKED_SUFFIXES",
    "MAX_URL_LENGTH",
    "NormalizedUrl",
    "UrlRejectedError",
    "validate_source_url",
]
