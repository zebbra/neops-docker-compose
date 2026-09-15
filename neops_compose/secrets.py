from __future__ import annotations

import datetime as dt
import os
import secrets as pysecrets
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CLIENT_SECRET_KEY = "NEOPS_KEYCLOAK_CLIENT_SECRET"


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write_secret(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write with the final mode set from creation, never a window at the umask default."""
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, mode)  # a pre-existing file keeps its old mode unless re-applied here


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


def read_env_value(path: Path, key: str) -> str | None:
    """One KEY=VALUE line out of a generated env file; None when the file or the key is absent.

    These files are written by write_secret, one key each, so a plain line scan is enough.
    """
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip() or None
    return None


def read_keycloak_client_secret(path: Path) -> str | None:
    return read_env_value(path, CLIENT_SECRET_KEY)


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
    if not cert_path.exists():
        return set(hosts)
    return set(hosts) - selfsigned_sans(cert_path)


CA_COMMON_NAME = "NeOps deployment CA"
CA_DAYS = 3650
BACKDATE = dt.timedelta(minutes=5)  # tolerate a few minutes of clock skew on the first start


def _mint_ca(now: dt.datetime) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Long-lived: this CA never leaves the deployment it was minted for."""
    key = _rsa_key()
    public_key = key.public_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME)])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - BACKDATE)
        .not_valid_after(now + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .sign(key, hashes.SHA256())
    )
    return ca, key


def _mint_leaf(
    hosts: list[str], ca: x509.Certificate, ca_key: rsa.RSAPrivateKey, now: dt.datetime, days: int
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """One server certificate carrying every public hostname as a SAN."""
    key = _rsa_key()
    public_key = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
        .issuer_name(ca.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - BACKDATE)
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


def ensure_selfsigned(tls_dir: Path, hosts: list[str], rotate: bool = False, days: int = 397) -> bool:
    """A private CA plus one server certificate for every public hostname. Never overwrites.

    days defaults to 397, Apple's limit on publicly-trusted leaf validity.
    """
    if not hosts:
        raise ValueError("ensure_selfsigned requires at least one host")
    cert_path, key_path = tls_dir / "cert.pem", tls_dir / "key.pem"
    if cert_path.exists() and key_path.exists() and not rotate:
        return False
    now = dt.datetime.now(dt.UTC)
    ca, ca_key = _mint_ca(now)
    cert, key = _mint_leaf(sorted(set(hosts)), ca, ca_key, now, days)
    pem = serialization.Encoding.PEM
    write_secret(tls_dir / "ca.pem", ca.public_bytes(pem))
    write_secret(key_path, _pem_private(key))
    write_secret(cert_path, cert.public_bytes(pem) + ca.public_bytes(pem))
    return True
