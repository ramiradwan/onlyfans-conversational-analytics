"""Authenticated retention without execution or implicit replay."""

from contextlib import contextmanager
from dataclasses import dataclass
from app.core.optional_execution import require_optional_execution
from app.models.text_analysis_client import ClientIntentPut
from app.persistence.auth import (
    SQLiteAuthenticationStore,
    AuthenticationStateError,
    RevocationKey,
    RevocationScopeType,
)
from app.persistence.authoritative_pair import AuthoritativePairOwner
from app.persistence.contributions import ContributionRepository
from app.persistence.text_analysis_client import TextAnalysisClientStore
from app.security.runtime_policy import RuntimeAuthorizationDenied, authorized_account
from app.security.text_analysis_identity import TextAnalysisIdentityOwner


class TextAnalysisClientService:
    def __init__(self, authentication, repository, pair_owner, dependencies):
        require_optional_execution()
        if (
            type(authentication) is not SQLiteAuthenticationStore
            or type(repository) is not ContributionRepository
            or type(pair_owner) is not AuthoritativePairOwner
            or pair_owner.authentication is not authentication
            or pair_owner.canonical_database is not repository.database
            or dependencies.persistence.authentication is not authentication
            or dependencies.source.history.database is not repository.database
        ):
            raise ValueError('text_analysis_client_owner_invalid')
        self.authentication, self.repository, self.pair_owner, self.dependencies = (
            authentication,
            repository,
            pair_owner,
            dependencies,
        )
        self.identity = TextAnalysisIdentityOwner(authentication)
        self.store = TextAnalysisClientStore(repository, clock=authentication._clock)

    def _current(self, db, original, baseline):
        auth = self.authentication
        try:
            session = auth._require_session_current(
                db, original.identity.session_id, auth._clock()
            )
            if (
                (
                    session['principal_id'],
                    session['creator_account_id'],
                    session['role'],
                )
                != (
                    original.identity.principal_id,
                    original.identity.creator_account_id,
                    original.identity.role,
                )
                or set(auth._session_grants(db, original.identity.session_id))
                != set(original.signed_object_reference_ids)
                or not auth.runtime_policy_is_current_in(
                    db, original, now=auth._clock()
                )
                or not auth.runtime_policy_is_current_in(
                    db, baseline, now=auth._clock()
                )
            ):
                raise AuthenticationStateError('Session changed')
        except AuthenticationStateError:
            raise RuntimeAuthorizationDenied(
                'text_analysis_session_unavailable'
            ) from None
        authorized_account(baseline, baseline.identity.creator_account_id)
        authority = baseline.identity_authority
        if (
            authority is None
            or authority.installation_id != self.pair_owner.installation_id
            or any(
                auth._scope_is_revoked(db, RevocationKey(kind, value))
                for kind, value in (
                    (RevocationScopeType.PRINCIPAL, baseline.identity.principal_id),
                    (
                        RevocationScopeType.CREATOR_ACCOUNT,
                        baseline.identity.creator_account_id,
                    ),
                    (RevocationScopeType.INSTALLATION, authority.installation_id),
                )
            )
        ):
            raise RuntimeAuthorizationDenied('text_analysis_identity_unavailable')

    @contextmanager
    def _transaction(self, policy):
        with self.dependencies.gate:
            identity = self.identity.capture(policy)
            baseline = self.identity.rebuild(identity)
            with self.authentication.database.transaction() as auth_db:
                self.pair_owner.validate_auth_in(auth_db)
                with self.repository.database.transaction() as db:
                    self._current(auth_db, policy, baseline)
                    yield auth_db, db, baseline
                    self._current(auth_db, policy, baseline)
                    self.pair_owner.validate_auth_in(auth_db)

    def retain(self, policy, intent):
        if type(intent) is not ClientIntentPut:
            raise ValueError('text_analysis_client_invalid')
        with self._transaction(policy) as (auth_db, db, baseline):
            old = self.store.read_in(db, intent.kind, intent.key, policy=baseline)
            if old is not None:
                from app.nlp.profiles import canonical_bytes

                if canonical_bytes(old.body) != canonical_bytes(intent.body):
                    raise ValueError('text_analysis_client_conflict')
                return old
            self.pair_owner.validate_in(auth_db, db)
            record = self.store.retain_in(db, intent, policy=baseline)
            self.pair_owner.validate_in(auth_db, db)
            return record

    def read(self, policy, kind, key):
        with self._transaction(policy) as (_, db, baseline):
            return self.store.read_in(db, kind, key, policy=baseline)

    def list(self, policy, *, after=None, limit=64):
        with self._transaction(policy) as (_, db, baseline):
            return self.store.list_in(db, policy=baseline, after=after, limit=limit)


@dataclass(frozen=True, slots=True)
class FixtureTextAnalysisClientConfiguration:
    authentication: SQLiteAuthenticationStore
    repository: ContributionRepository
    pair_owner: AuthoritativePairOwner

    def bind(self, source, dependencies):
        require_optional_execution()
        if (
            source is not dependencies.source
            or source.history.database is not self.repository.database
        ):
            raise ValueError('text_analysis_client_owner_invalid')
        return TextAnalysisClientService(
            self.authentication, self.repository, self.pair_owner, dependencies
        )


_service = None


def client_service():
    return _service


def configure_client_service(service):
    global _service
    if service is not None and type(service) is not TextAnalysisClientService:
        raise ValueError('text_analysis_client_service_invalid')
    _service = service
