"""URL validation is a security boundary, so it is tested as one.

These are unit tests: no database, no network. `validate_source_url` performs no I/O
by design, and a test that needed a socket would mean it had started to.
"""

from __future__ import annotations

import pytest

from app.domains.onboarding.urls import (
    ALLOWED_PORTS,
    ALLOWED_SCHEMES,
    MAX_URL_LENGTH,
    UrlRejectedError,
    validate_source_url,
)


class TestSchemesRejected:
    """Step 4 requirement 15: only http/https may ever be stored."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "file://C:/Windows/win.ini",
            "ftp://ftp.example.ac.uk/prospectus.pdf",
            "gopher://example.ac.uk:70/1",
            "data:text/html;base64,PHNjcmlwdD4=",
            "javascript:alert(1)",
            "jar:http://example.ac.uk!/",
            "dict://example.ac.uk:2628/",
            "ldap://example.ac.uk/",
            "sftp://example.ac.uk/",
            "redis://127.0.0.1:6379",
            "//example.ac.uk/no-scheme",
            "example.ac.uk/no-scheme-at-all",
        ],
    )
    def test_non_http_schemes_cannot_be_stored(self, url: str) -> None:
        with pytest.raises(UrlRejectedError):
            validate_source_url(url)

    def test_only_two_schemes_are_permitted(self) -> None:
        assert set(ALLOWED_SCHEMES) == {"http", "https"}


class TestAuthorityRejected:
    @pytest.mark.parametrize(
        "url",
        [
            # Loopback, link-local (cloud metadata), private ranges and 0.0.0.0.
            "http://127.0.0.1/",
            "http://127.1/",
            "http://0.0.0.0/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            # Decimal and octal spellings of loopback.
            "http://2130706433/",
            "http://0177.0.0.1/",
            # IPv6, including the bracketed loopback.
            "http://[::1]/",
            "http://[fe80::1]/",
            "http://[0:0:0:0:0:ffff:127.0.0.1]/",
        ],
    )
    def test_ip_literals_are_rejected(self, url: str) -> None:
        """An IP cannot be shown to belong to an institution by any recorded method."""
        with pytest.raises(UrlRejectedError):
            validate_source_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/",
            "http://localhost:8080/admin",
            "http://api.internal/",
            "http://db.intranet/",
            "http://printer.local/",
            "http://something.localdomain/",
            "http://host.test/",
            "http://host.example/",
            "http://host.invalid/",
            "http://abcdefg.onion/",
        ],
    )
    def test_local_and_reserved_names_are_rejected(self, url: str) -> None:
        with pytest.raises(UrlRejectedError):
            validate_source_url(url)

    def test_a_blocked_name_is_not_matched_as_a_substring(self) -> None:
        """`localhost` must not reject a real institution whose name contains it."""
        result = validate_source_url("https://localhost-college.ac.uk/")
        assert result.host == "localhost-college.ac.uk"

    @pytest.mark.parametrize(
        "url",
        [
            "http://user:pass@example.ac.uk/",
            "http://user@example.ac.uk/",
            "https://admin:secret@admissions.example.ac.uk/fees",
            # The classic confusion: everything before '@' is credentials, so this
            # actually addresses 169.254.169.254.
            "http://example.ac.uk@169.254.169.254/",
        ],
    )
    def test_embedded_credentials_are_rejected(self, url: str) -> None:
        with pytest.raises(UrlRejectedError):
            validate_source_url(url)

    def test_missing_host_is_rejected(self) -> None:
        with pytest.raises(UrlRejectedError):
            validate_source_url("http:///path-only")

    def test_single_label_host_is_rejected(self) -> None:
        with pytest.raises(UrlRejectedError):
            validate_source_url("http://intranet/")


class TestPorts:
    @pytest.mark.parametrize("port", sorted(ALLOWED_PORTS))
    def test_web_ports_are_accepted(self, port: int) -> None:
        assert validate_source_url(f"https://example.ac.uk:{port}/").host == "example.ac.uk"

    @pytest.mark.parametrize("url", ["http://example.ac.uk:22/", "http://example.ac.uk:6379/"])
    def test_other_ports_are_rejected(self, url: str) -> None:
        with pytest.raises(UrlRejectedError):
            validate_source_url(url)

    def test_default_port_is_dropped_so_one_page_is_one_source(self) -> None:
        """http://x and http://x:80 address the same page and must normalise alike."""
        plain = validate_source_url("http://example.ac.uk/fees")
        explicit = validate_source_url("http://example.ac.uk:80/fees")
        assert plain.normalized == explicit.normalized
        assert plain.sha256 == explicit.sha256
        assert plain.port is None

    def test_a_non_default_port_is_kept(self) -> None:
        result = validate_source_url("https://example.ac.uk:8443/fees")
        assert result.port == 8443
        assert result.normalized == "https://example.ac.uk:8443/fees"


class TestMalformed:
    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "https://exa mple.ac.uk/",
            "https://example.ac.uk/a\nb",
            "https://example.ac.uk/a\rb",
            "https://example.ac.uk/\x00",
            "https://example.ac.uk/\tb",
        ],
    )
    def test_empty_and_control_characters_are_rejected(self, url: str) -> None:
        with pytest.raises(UrlRejectedError):
            validate_source_url(url)

    def test_over_long_urls_are_rejected(self) -> None:
        url = "https://example.ac.uk/" + "a" * MAX_URL_LENGTH
        with pytest.raises(UrlRejectedError, match="exceeds"):
            validate_source_url(url)

    def test_a_non_string_is_rejected_rather_than_coerced(self) -> None:
        with pytest.raises(UrlRejectedError):
            validate_source_url(None)  # type: ignore[arg-type]


class TestNormalisation:
    def test_scheme_and_host_are_lowercased(self) -> None:
        result = validate_source_url("HTTPS://WWW.Example.AC.UK/Fees")
        assert result.scheme == "https"
        assert result.host == "www.example.ac.uk"
        # The path keeps its case: many servers are case-sensitive on paths.
        assert result.normalized == "https://www.example.ac.uk/Fees"

    def test_fragment_is_discarded_because_a_server_never_sees_it(self) -> None:
        with_fragment = validate_source_url("https://example.ac.uk/fees#international")
        without = validate_source_url("https://example.ac.uk/fees")
        assert with_fragment.normalized == without.normalized

    def test_query_is_preserved_exactly(self) -> None:
        """Query-addressed pages are common; 'tidying' one fetches a different page."""
        result = validate_source_url("https://example.ac.uk/search?level=pg&subject=cs")
        assert result.normalized == "https://example.ac.uk/search?level=pg&subject=cs"

    def test_empty_path_becomes_root(self) -> None:
        assert validate_source_url("https://example.ac.uk").normalized == "https://example.ac.uk/"

    def test_trailing_dot_on_host_is_removed(self) -> None:
        assert validate_source_url("https://example.ac.uk./").host == "example.ac.uk"

    def test_dot_segments_are_not_collapsed(self) -> None:
        """Rewriting a path would register a URL nobody chose."""
        result = validate_source_url("https://example.ac.uk/a/../b")
        assert "/a/../b" in result.normalized

    def test_internationalised_host_is_idna_encoded(self) -> None:
        """Validated as the ASCII name that will actually be resolved."""
        result = validate_source_url("https://universität.example.de/")
        assert result.host.startswith("xn--")
        assert result.host.isascii()

    def test_hash_is_over_the_normalised_form(self) -> None:
        import hashlib

        result = validate_source_url("HTTPS://Example.AC.UK/fees")
        expected = hashlib.sha256(result.normalized.encode("utf-8")).hexdigest()
        assert result.sha256 == expected

    def test_original_is_kept_verbatim(self) -> None:
        """What the operator typed stays recoverable, for a support conversation."""
        raw = "  HTTPS://Example.AC.UK/fees#x  "
        result = validate_source_url(raw)
        assert result.original == raw.strip()


class TestRealisticUniversityUrls:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.imperial.ac.uk/study/courses/",
            "https://www.ucl.ac.uk/prospective-students/graduate",
            "https://www.hku.hk/admission/",
            "https://www.um.edu.mo/admissions/",
            "https://www.ox.ac.uk/admissions/graduate/courses?page=1",
            "https://www.ed.ac.uk/files/atoms/files/fees.pdf",
            "http://www.example.ac.uk/legacy-page",
        ],
    )
    def test_ordinary_official_urls_are_accepted(self, url: str) -> None:
        result = validate_source_url(url)
        assert result.scheme in ALLOWED_SCHEMES
        assert result.host
        assert result.sha256
