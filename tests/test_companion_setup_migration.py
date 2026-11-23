"""Preserve unambiguous setup evidence across the first-configuration migration."""

from pathlib import Path
from shutil import copyfile

import pytest

from app.persistence import auth

pytestmark = [pytest.mark.ci_tier('fast'), pytest.mark.windows_compat]


@pytest.mark.parametrize(('confirmation', 'other_revocation', 'ready'), [
    ('099999', None, True),
    ('100900', None, False),
    ('099999', '100200', False),
    ('099999', '099999', True),
])
def test_upgrade_matches_whole_millisecond_to_one_pin(tmp_path, confirmation, other_revocation, ready):
    catalog = Path(auth.__file__).with_name('auth_sql')
    previous = tmp_path / 'version54'
    previous.mkdir()
    for source in catalog.glob('*.sql'):
        if int(source.name[:4]) <= 54:
            copyfile(source, previous / source.name)
    path = tmp_path / 'auth.sqlite3'
    old = auth.SQLiteAuthenticationStore(path, migrations_dir=previous)
    prefix = '2026-03-15T10:00:00.'
    with old.database.transaction() as connection:
        for key, confirmed, revoked in [('active', confirmation, None), ('old', '000000', other_revocation)]:
            if key == 'old' and revoked is None:
                continue
            connection.execute(
                'INSERT INTO agent_pairings (pairing_id,key_id,principal_id,creator_account_id,agent_installation_id,'
                'external_issuer,external_subject,installation_id,public_key,key_fingerprint,created_at,confirmed_at,'
                'revoked_at,companion_confirmation_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, key, 'agent', 'creator', key, 'issuer', 'subject', 'install', b'key', 'fingerprint',
                 prefix + '000000+00:00', prefix + confirmed + '+00:00',
                 None if revoked is None else prefix + revoked + '+00:00', 'browser_verified'),
            )
        connection.execute(
            'INSERT INTO onboarding_progress_outbox (milestone,event_id,occurred_at,onboarding_transaction_id,'
            'organization_id,installation_id,correlation_id,next_attempt_at) VALUES (?,?,?,?,?,?,?,?)',
            ('first-capture-ready', 'event', prefix + '100Z', 'transaction', 'org', 'install', 'request', prefix + '100Z'),
        )
    current = auth.SQLiteAuthenticationStore(path)
    with current.database.read() as connection:
        stamp = connection.execute("SELECT first_configuration_at FROM agent_pairings WHERE pairing_id='active'").fetchone()[0]
    assert stamp == (prefix + '100999+00:00' if ready else None)
