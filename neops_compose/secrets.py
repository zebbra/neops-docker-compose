from __future__ import annotations

import datetime as dt
import os
import secrets as pysecrets
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CLIENT_SECRET_KEY = "NEOPS_KEYCLOAK_CLIENT_SECRET"


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write_secret(path: Path, data: bytes) -> None:
    private_dir(path.parent)
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_private(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def ensure_jwt(jwt_dir: Path) -> bool:
    """RSA-2048 keypair the CMS signs tokens with and the engine verifies. Never overwrites."""
    priv, pub = jwt_dir / "private.pem", jwt_dir / "public.pem"
    if priv.exists() and pub.exists():
        return False
    key = _rsa_key()
    write_secret(priv, _pem_private(key))
    write_secret(
        pub,
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    )
    return True


def read_keycloak_client_secret(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if line.startswith(CLIENT_SECRET_KEY + "="):
            return line.split("=", 1)[1].strip()
    return None


def ensure_keycloak_client_secret(path: Path, rotate: bool = False) -> str:
    existing = read_keycloak_client_secret(path)
    if existing and not rotate:
        return existing
    value = pysecrets.token_hex(32)
    write_secret(path, f"{CLIENT_SECRET_KEY}={value}\n".encode())
    return value


def selfsigned_sans(cert_path: Path) -> set[str]:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return set(ext.value.get_values_for_type(x509.DNSName))


def stale_sans(cert_path: Path, hosts: list[str]) -> set[str]:
    return set(hosts) - selfsigned_sans(cert_path)


def ensure_selfsigned(tls_dir: Path, hosts: list[str], rotate: bool = False, days: int = 1095) -> bool:
    """A private CA plus one server certificate carrying every public hostname as a SAN."""
    cert_path = tls_dir / "cert.pem"
    if cert_path.exists() and not rotate:
        return False
    now = dt.datetime.now(dt.UTC)
    ca_key = _rsa_key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NeOps deployment CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days * 2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = _rsa_key()
    hosts = sorted(set(hosts))
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    write_secret(tls_dir / "ca.pem", ca.public_bytes(pem))
    write_secret(tls_dir / "key.pem", _pem_private(key))
    write_secret(cert_path, cert.public_bytes(pem) + ca.public_bytes(pem))
    return True
