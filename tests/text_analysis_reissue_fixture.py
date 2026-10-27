"""Signed local identity, real wire client and encrypted reissue journal."""
from dataclasses import replace
from types import SimpleNamespace

from app.persistence.auth import SQLiteAuthenticationStore, InstallationKeyReservation
from app.security.text_analysis_contract import TextAnalysisSchemas
from app.services.text_analysis_reissue import TextAnalysisReissueService
from tests.test_text_analysis_identity import make_identity_fixture
from tests.test_text_analysis_reissue import NOW
from tests.test_text_analysis_reissue_transport import make_reissue_client


def make_reissue_fixture(path, local_signer, *, now=NOW, origin='https://example.test', reason=None):
    schemas = TextAnalysisSchemas()
    client, context, authority_context, requests, challenges, mode, clock = make_reissue_client(
        schemas, local_signer, origin=origin, now=now, reason=reason)
    auth = SQLiteAuthenticationStore(path/'auth.sqlite3', clock=lambda: clock[0])
    key = client.key_authority.ensure_ready()
    auth.reserve_installation_key(InstallationKeyReservation(key.provider_name, key.provider_key_name,
        key.algorithm, key.created_at))
    auth.activate_installation_key(key)
    identity = make_identity_fixture(path/'identity', authentication=auth, scope=context.binding, now=now)
    def original(policy, retained_key):
        assert retained_key == key
        return replace(context, operation_expires_at=None)
    def reopen(authentication=None, *, context_factory=original):
        return TextAnalysisReissueService(authentication or auth, client, provider_origin=origin,
            context_factory=context_factory, authority_context_factory=lambda confirmation: authority_context,
            clock=lambda: clock[0])
    owner = reopen()
    return SimpleNamespace(auth=auth, service=owner, client=client, context=context, key=key,
        identity=identity, requests=requests, challenges=challenges, mode=mode, clock=clock,
        authority_context=authority_context, reopen=reopen,
        http_policy=lambda: auth.read_bridge_session(identity.session.session_value).policy)
