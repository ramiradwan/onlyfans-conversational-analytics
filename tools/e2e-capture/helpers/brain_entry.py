"""E2E-only Brain entry that supplies a signer for the synthetic installation key.

The application and every pairing/session endpoint remain production modules.
Only the installation proof signer is replaced when the disposable E2E store
contains the explicitly named synthetic provider used by seed_webauthn_grants.
"""

from __future__ import annotations

import sys
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[3]
HELPER_ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(PRODUCT_ROOT), str(HELPER_ROOT)]

import uvicorn  # noqa: E402

from app.api.endpoints import companion_pairing  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.persistence.auth import SQLiteAuthenticationStore  # noqa: E402
from app.security.companion_pairing import CompanionPairingService  # noqa: E402
from pairing_fixture import SyntheticPairingAuthority  # noqa: E402


SYNTHETIC_PROVIDER_NAME = "E2E Synthetic Installation Key Provider"
_original_pairing_service = companion_pairing.companion_pairing_service


def _e2e_pairing_service() -> CompanionPairingService:
    store = SQLiteAuthenticationStore(settings.auth_database_path)
    reference = store.installation_key_reference()
    if reference is None or reference.provider_name != SYNTHETIC_PROVIDER_NAME:
        return _original_pairing_service()
    return CompanionPairingService(store, SyntheticPairingAuthority(reference))


companion_pairing.companion_pairing_service = _e2e_pairing_service


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=17871,
        workers=1,
        access_log=False,
        log_level="warning",
    )
