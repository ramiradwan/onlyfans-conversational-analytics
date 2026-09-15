from __future__ import annotations


import pytest

from app.core.config import settings
from app.bootstrap import transport_manager
from app.security.extension_storage import (
    extension_storage_key,
    open_extension_storage_bootstrap,
    seal_extension_storage_bootstrap,
)
from app.security.local_data_key import LocalDataKeyError
from app.transport.manager import DEV_ACCOUNT_ID


EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
OTHER_EXTENSION_ID = "bcdefghijklmnopabcdefghijklmnopa"


@pytest.fixture(autouse=True)
def reset_transport() -> None:
    transport_manager.reset()
    yield
    transport_manager.reset()


def bootstrap(account_id: str = DEV_ACCOUNT_ID, ticket: str = "pair-secret") -> str:
    return seal_extension_storage_bootstrap(
        extension_id=EXTENSION_ID,
        creator_account_id=account_id,
        credential_kind="pairing",
        auth_ticket=ticket,
    )


def test_bootstrap_is_opaque_device_bound_and_account_keys_are_separated() -> None:
    sealed = bootstrap()
    assert DEV_ACCOUNT_ID not in sealed
    assert "pair-secret" not in sealed
    assert open_extension_storage_bootstrap(
        sealed,
        expected_extension_id=EXTENSION_ID,
    ).auth_ticket == "pair-secret"
    with pytest.raises(LocalDataKeyError):
        open_extension_storage_bootstrap(
            sealed,
            expected_extension_id=OTHER_EXTENSION_ID,
        )
    tampered = sealed[:-1] + ("A" if sealed[-1] != "A" else "B")
    with pytest.raises(LocalDataKeyError):
        open_extension_storage_bootstrap(
            tampered,
            expected_extension_id=EXTENSION_ID,
        )

    first = extension_storage_key(
        settings.auth_database_path,
        extension_id=EXTENSION_ID,
        creator_account_id=DEV_ACCOUNT_ID,
    )
    repeated = extension_storage_key(
        settings.auth_database_path,
        extension_id=EXTENSION_ID,
        creator_account_id=DEV_ACCOUNT_ID,
    )
    other = extension_storage_key(
        settings.auth_database_path,
        extension_id=EXTENSION_ID,
        creator_account_id="other-account",
    )
    assert first == repeated
    assert first != other
    assert len(first) == 32
