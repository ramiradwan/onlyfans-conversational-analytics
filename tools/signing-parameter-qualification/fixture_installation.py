"""Public contract installation-key test double for an isolated QA artifact.

This is software, not TPM evidence. It models the provider interface so the
normal installation-key, pairing and activation policy checks still run. No
grant or publication producer is imported here. Never include it in shipping
Brain; only the explicit qualification build may collect this module.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, decode_dss_signature

from app.security.installation_key import (
    INSTALLATION_KEY_ALGORITHM,
    PLATFORM_CRYPTO_PROVIDER,
    InstallationKeyUnavailable,
    ProviderKeyInfo,
)

ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
PREFIX = "OFCA TEST VECTORS ONLY - NEVER PRODUCTION - "
SESSION_PURPOSE = "signing-parameter-qa-operator-session"


class SyntheticInstallationProvider:
    """Reconstructable published fixture key; no hardware security claim."""

    provider_name = PLATFORM_CRYPTO_PROVIDER

    def __init__(self, key_name: str | None = None):
        material = hashlib.sha256((PREFIX + "installation-proof").encode("ascii")).digest()
        self._key = ec.derive_private_key(
            int.from_bytes(material, "big") % (ORDER - 1) + 1, ec.SECP256R1()
        )
        self.key_name = key_name

    def key_info(self, provider_key_name: str):
        if provider_key_name != self.key_name or self.key_name is None:
            return None
        numbers = self._key.public_key().public_numbers()
        return ProviderKeyInfo(
            provider_name=self.provider_name,
            provider_key_name=provider_key_name,
            algorithm=INSTALLATION_KEY_ALGORITHM,
            export_policy=0,
            # Interface simulation only; receipt explicitly records software.
            hardware_backed=True,
            x=numbers.x.to_bytes(32, "big"),
            y=numbers.y.to_bytes(32, "big"),
        )

    def create_non_exportable_key(self, provider_key_name: str):
        if self.key_name is not None:
            raise InstallationKeyUnavailable("Synthetic installation key already exists")
        self.key_name = provider_key_name
        return self.key_info(provider_key_name)

    def sign_digest(self, provider_key_name: str, digest: bytes) -> bytes:
        if self.key_info(provider_key_name) is None:
            raise InstallationKeyUnavailable("Synthetic installation key is unavailable")
        encoded = self._key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
        r, s = decode_dss_signature(encoded)
        return r.to_bytes(32, "big") + min(s, ORDER - s).to_bytes(32, "big")


def open_fixture_provider(store):
    reference = store.installation_key_reference()
    if reference is None:
        raise InstallationKeyUnavailable("Synthetic installation admission is absent")
    return SyntheticInstallationProvider(reference.provider_key_name)


def open_admitted_context(directory: str | Path):
    """Open durable admission and a protected, still-current operator session."""
    from app.persistence.auth import SQLiteAuthenticationStore
    from app.security.account_bindings import eligible_account_for_policy
    from app.security.companion_pairing import CompanionPairingService
    from app.security.installation_key import InstallationKeyAuthority
    from app.security.local_data_key import unprotect_local_secret

    root = Path(directory).resolve()
    receipt = json.loads((root / "admission.json").read_bytes())
    if receipt.get("qualification_only") is not True or receipt.get("production_usable") is not False:
        raise ValueError("Qualification admission is absent")
    store = SQLiteAuthenticationStore(root / "auth.sqlite3")
    secrets = json.loads(unprotect_local_secret((root / "operator-session.dpapi").read_bytes(), purpose=SESSION_PURPOSE))
    def refresh_policy():
        # Match normal HTTP admission: authenticate the same session credential
        # on each operation, rather than reusing a stale global-epoch snapshot.
        session = store.read_bridge_session(secrets["session_value"])
        if session is None:
            raise ValueError("Qualification operator session is not current")
        eligible_account_for_policy(store, session.policy, now=datetime.now(timezone.utc))
        return session.policy

    policy = refresh_policy()
    account = eligible_account_for_policy(store, policy, now=datetime.now(timezone.utc))
    authority = InstallationKeyAuthority(store, open_fixture_provider(store))
    return SimpleNamespace(store=store, authority=authority,
                           service=CompanionPairingService(store, authority), policy=policy,
                           account=account.creator_account_id, receipt=receipt, csrf_value=secrets["csrf_value"],
                           refresh_policy=refresh_policy)
