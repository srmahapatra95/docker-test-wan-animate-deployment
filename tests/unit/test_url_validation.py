from __future__ import annotations

import socket

import pytest

from tests.support import handler


@pytest.mark.parametrize("url", [None, "", "   ", 42])
def test_missing_or_non_string_url_is_rejected(url):
    with pytest.raises(handler.URLValidationError, match="non-empty input file URL"):
        handler._validate_url(url)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://example.com/file.mp4", "Only HTTPS URLs are accepted"),
        ("https:///file.mp4", "must contain a hostname"),
        ("https://user:password@example.com/file.mp4", "Credentials embedded"),
        ("https://example.com:8443/file.mp4", "standard HTTPS port"),
        ("https://example.com:invalid/file.mp4", "invalid port"),
    ],
)
def test_invalid_url_components_are_rejected(url, message):
    with pytest.raises(handler.URLValidationError, match=message):
        handler._validate_url(url)


@pytest.mark.parametrize("url", ["https://localhost/file.mp4", "https://api.localhost/file.mp4"])
def test_localhost_urls_are_rejected_without_dns_lookup(url, monkeypatch):
    def unexpected_dns_lookup(*args, **kwargs):
        pytest.fail("localhost must be rejected before DNS resolution")

    monkeypatch.setattr(handler.socket, "getaddrinfo", unexpected_dns_lookup)

    with pytest.raises(handler.URLValidationError, match="Localhost URLs are not allowed"):
        handler._validate_url(url)


@pytest.mark.parametrize("url", ["https://127.0.0.1/file.mp4", "https://10.0.0.1/file.mp4"])
def test_private_ip_addresses_are_rejected(url):
    with pytest.raises(handler.URLValidationError, match="private or reserved IP addresses"):
        handler._validate_url(url)


def test_hostname_resolving_to_private_address_is_rejected(monkeypatch):
    monkeypatch.setattr(
        handler.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))],
    )

    with pytest.raises(handler.URLValidationError, match="private or reserved IP addresses"):
        handler._validate_url("https://media.example.com/file.mp4")


def test_unresolvable_hostname_is_rejected(monkeypatch):
    def dns_failure(*args, **kwargs):
        raise socket.gaierror("DNS lookup failed")

    monkeypatch.setattr(handler.socket, "getaddrinfo", dns_failure)

    with pytest.raises(handler.URLValidationError, match="hostname could not be resolved"):
        handler._validate_url("https://media.example.com/file.mp4")


def test_hostname_with_no_dns_addresses_is_rejected(monkeypatch):
    monkeypatch.setattr(handler.socket, "getaddrinfo", lambda *args, **kwargs: [])

    with pytest.raises(handler.URLValidationError, match="hostname did not resolve"):
        handler._validate_url("https://media.example.com/file.mp4")


def test_https_url_with_public_dns_address_is_accepted(monkeypatch):
    monkeypatch.setattr(
        handler.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )

    parsed = handler._validate_url(" HTTPS://media.example.com:443/file.mp4 ")

    assert parsed.scheme == "https"
    assert parsed.hostname == "media.example.com"
    assert parsed.port == 443
