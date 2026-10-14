"""Create an isolated synthetic TLS fixture, never publication or production keys.

No CA is installed. The browser controller trusts only this fixture's SPKI in
its disposable profile. The private server key stays outside the checkout.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.persistence.private_files import apply_private_file_security, reject_path_aliases  # noqa: E402


def prepare(directory: Path) -> dict:
    target = reject_path_aliases(directory.absolute())
    if target == ROOT or ROOT in target.parents or target.exists():
        raise ValueError("Synthetic TLS requires a new directory outside the checkout")
    target.mkdir(parents=True)
    apply_private_file_security(target)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PUBLIC SYNTHETIC QUALIFICATION ONLY"),
        x509.NameAttribute(NameOID.COMMON_NAME, "onlyfans.com synthetic fixture"),
    ])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("onlyfans.com")]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256()))
    key_path = target / "synthetic-onlyfans-key.pem"
    with key_path.open("xb") as handle:
        apply_private_file_security(key_path)
        handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
        handle.flush()
        os.fsync(handle.fileno())
    certificate_path = target / "synthetic-onlyfans-cert.pem"
    public_bytes = certificate.public_bytes(serialization.Encoding.PEM)
    certificate_path.write_bytes(public_bytes)
    spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    receipt = {
        "qualification_only": True, "production_usable": False, "ca_installed": False,
        "certificate": str(certificate_path), "private_key_path": str(key_path),
        "certificate_sha256": hashlib.sha256(public_bytes).hexdigest(),
        "spki_sha256_base64": base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii"),
    }
    (target / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    options = parser.parse_args()
    print(json.dumps(prepare(options.directory), sort_keys=True))
