"""Production transport composition with durable platform identity authority."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from app.core.config import settings
from app.persistence import sqlite_api as sqlite3
from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.factory import CanonicalRepositories
from app.provisioning.progress_reporting import OnboardingProgressCoordinator
from app.security.account_bindings import eligible_accounts
from app.services.agent_configuration import AgentConfigurationAuthority
from app.transport.manager import InMemoryTransportManager, authorized_account_ids


@lru_cache(maxsize=4)
def _identity_store(path: str) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(Path(path))


def authorized_platform_identities() -> Mapping[str, str]:
    """Return durable creator-account to platform-identity pins.

    Eligibility is resolved from the same current grant-backed account bindings
    used for account authorization. Failure to resolve the store yields no pins,
    which makes configuration publication fail closed rather than adopting a
    browser-observed identity as authority.
    """

    try:
        store = _identity_store(str(settings.auth_database_path))
        accounts = eligible_accounts(store, now=datetime.now(timezone.utc))
    except (OSError, ValueError, sqlite3.Error):
        return {}
    return {
        account.creator_account_id: account.platform_creator_id
        for account in accounts
    }


class AccountBoundTransportManager(InMemoryTransportManager):
    """Transport manager whose Agent configuration carries durable identity pins."""

    def __init__(
        self,
        repositories: CanonicalRepositories,
        *,
        onboarding_progress: OnboardingProgressCoordinator | None = None,
    ) -> None:
        super().__init__(repositories, onboarding_progress=onboarding_progress)
        self.config_authority = AgentConfigurationAuthority(
            repositories.configuration,
            authorized_accounts=authorized_account_ids,
            authorized_platform_identities=authorized_platform_identities,
        )
