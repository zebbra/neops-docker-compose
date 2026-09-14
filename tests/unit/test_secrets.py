# tests/unit/test_secrets.py
import stat

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from neops_compose import secrets


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_ensure_jwt_creates_pkcs8_and_spki_once(tmp_path):
    d = tmp_path / "jwt"
    assert secrets.ensure_jwt(d) is True
    priv, pub = d / "private.pem", d / "public.pem"
    assert priv.read_text().startswith("-----BEGIN PRIVATE KEY-----")
    assert pub.read_text().startswith("-----BEGIN PUBLIC KEY-----")
    assert mode(priv) == 0o600 and mode(d) == 0o700
    before = priv.read_bytes()
    assert secrets.ensure_jwt(d) is False and priv.read_bytes() == before
    key = serialization.load_pem_private_key(before, password=None)
    assert key.key_size == 2048


def test_keycloak_client_secret_is_stable(tmp_path):
    p = tmp_path / "keycloak-client.env"
    first = secrets.ensure_keycloak_client_secret(p)
    assert len(first) == 64 and p.read_text() == f"NEOPS_KEYCLOAK_CLIENT_SECRET={first}\n"
    assert secrets.ensure_keycloak_client_secret(p) == first
    assert secrets.read_keycloak_client_secret(p) == first and mode(p) == 0o600


def test_selfsigned_cert_covers_hosts_and_detects_staleness(tmp_path):
    d = tmp_path / "tls"
    assert secrets.ensure_selfsigned(d, ["neops.example.com", "cms.neops.example.com"]) is True
    cert = x509.load_pem_x509_certificate((d / "cert.pem").read_bytes())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.DNSName
    )
    assert set(sans) == {"neops.example.com", "cms.neops.example.com"}
    assert (d / "ca.pem").exists() and (d / "key.pem").exists() and mode(d / "key.pem") == 0o600
    assert secrets.selfsigned_sans(d / "cert.pem") == {"neops.example.com", "cms.neops.example.com"}
    assert secrets.stale_sans(d / "cert.pem", ["neops.example.com", "new.example.com"]) == {"new.example.com"}
    assert secrets.ensure_selfsigned(d, ["neops.example.com"]) is False  # never overwrites
    assert secrets.ensure_selfsigned(d, ["neops.example.com", "new.example.com"], rotate=True) is True
    assert secrets.stale_sans(d / "cert.pem", ["new.example.com"]) == set()


def test_write_secret_sets_mode_immediately_and_preserves_inode_on_rewrite(tmp_path):
    p = tmp_path / "sub" / "file.txt"
    secrets.write_secret(p, b"one", mode=0o644)
    assert mode(p) == 0o644
    ino = p.stat().st_ino
    secrets.write_secret(p, b"two-is-longer-than-one")
    assert p.stat().st_ino == ino
    assert p.read_bytes() == b"two-is-longer-than-one"
    assert mode(p) == 0o600


def test_ensure_selfsigned_requires_at_least_one_host(tmp_path):
    with pytest.raises(ValueError):
        secrets.ensure_selfsigned(tmp_path / "tls", [])


def test_ensure_selfsigned_regenerates_when_key_file_is_missing(tmp_path):
    d = tmp_path / "tls"
    secrets.ensure_selfsigned(d, ["neops.example.com"])
    (d / "key.pem").unlink()
    assert secrets.ensure_selfsigned(d, ["neops.example.com"]) is True
    assert (d / "key.pem").exists()


def test_selfsigned_leaf_certificate_has_server_extensions_and_397_day_validity(tmp_path):
    d = tmp_path / "tls"
    secrets.ensure_selfsigned(d, ["neops.example.com"])
    cert = x509.load_pem_x509_certificate((d / "cert.pem").read_bytes())
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in eku
    ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert ku.digital_signature and ku.key_encipherment
    cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    validity = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert validity.days == 397


def test_stale_sans_returns_all_hosts_when_cert_file_is_missing(tmp_path):
    hosts = ["a.example.com", "b.example.com"]
    assert secrets.stale_sans(tmp_path / "tls" / "cert.pem", hosts) == set(hosts)
