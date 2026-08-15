"""Durable completion of local provisioning.

The provisioning surface is isolated from the runtime modules finalization
needs, so the finalize action and the readiness read are built here and injected
into it. Finalization writes installation configuration first and the authorized
account second: an installation activates without any authorized account, so a
failure between the two writes leaves a bootable installation that has
authorized nothing, while the reverse order would leave a durable account
binding the store refuses to record twice and no configuration to boot into.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from app.core.first_run import FirstRunConflictError
from app.core.runtime_paths import runtime_configuration_file, runtime_data_directory
from app.persistence.auth import (
    AccountBindingRefused,
    AuthenticationStore,
    SQLiteAuthenticationStore,
)
from app.provisioning.finalize import (
    FinalizationRefused,
    FinalizationRequest,
    authorize_finalized_account,
    finalize_provisioning,
)


# The path installation configuration pins as AUTH_DATABASE_PATH. Provisioning
# writes the same file, so finalization does not move durable state.
AUTHENTICATION_DATABASE_FILENAME = "auth.sqlite3"

CompletionRefusal = Literal[
    "configuration_conflict",
    "installation_identity_unavailable",
]


def durable_authentication_store(
    data_directory: str | Path | None = None,
) -> Callable[[], AuthenticationStore]:
    """Open the provisioning-stage authentication store at most once."""

    opened: AuthenticationStore | None = None

    def open_store() -> AuthenticationStore:
        nonlocal opened
        if opened is None:
            directory = runtime_data_directory(data_directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            opened = SQLiteAuthenticationStore(
                directory / AUTHENTICATION_DATABASE_FILENAME
            )
        return opened

    return open_store


def durable_completion_reader(
    open_store: Callable[[], AuthenticationStore],
    *,
    data_directory: str | Path | None = None,
) -> Callable[[], bool]:
    """Report whether both finalization writes are present, writing nothing.

    The account record is read rather than resolved: the store refuses to record
    a second authorization, so a lapsed or revoked one still means finalization
    happened and cannot happen again. Eligibility is a separate question the
    runtime answers.
    """

    def provisioning_is_finalized() -> bool:
        if not runtime_configuration_file(data_directory).exists():
            return False
        return bool(open_store().authorized_account_bindings())

    return provisioning_is_finalized


def durable_finalize_action(
    open_store: Callable[[], AuthenticationStore],
    *,
    extension_id: str,
    data_directory: str | Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Callable[..., str | None]:
    """Build the action that finalizes provisioning and authorizes its account.

    Returns `None` when both writes land, and a nonsecret refusal reason
    otherwise. Nothing is written before a refusal that names the grant set, the
    approval, or the installation identity: those are decided from durable state
    that finalization only reads.
    """

    def complete_provisioning(
        *,
        association_request_id: str,
        detected_creator_account_id: str,
        reported_platform_creator_id: str | None,
    ) -> str | None:
        request = FinalizationRequest(
            association_request_id=association_request_id,
            detected_creator_account_id=detected_creator_account_id,
            reported_platform_creator_id=reported_platform_creator_id,
        )
        store = open_store()
        try:
            finalized = finalize_provisioning(
                store=store,
                request=request,
                extension_id=extension_id,
                data_directory=data_directory,
            )
            authorize_finalized_account(
                store=store,
                request=request,
                finalized=finalized,
                authorized_at=now(),
            )
        except FinalizationRefused as refusal:
            return refusal.reason
        except AccountBindingRefused as refusal:
            return refusal.reason
        except FirstRunConflictError:
            return "configuration_conflict"
        except ValueError:
            # The verified tuple cannot produce a valid installation identity,
            # which includes an absent or malformed extension identity.
            return "installation_identity_unavailable"
        return None

    return complete_provisioning
