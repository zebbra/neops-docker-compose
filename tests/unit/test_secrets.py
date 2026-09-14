# tests/unit/test_secrets.py
import stat

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
