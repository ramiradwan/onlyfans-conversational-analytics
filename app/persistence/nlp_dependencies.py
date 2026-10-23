"""Encrypted dependency authority and restart intents, independent of data cache."""
from __future__ import annotations

import hashlib
import json

from app.nlp.profiles import canonical_bytes, artifact_closure_digest, read_profile
from app.nlp.release_catalog import canonical, require, MAX_PROFILES


class DependencyPersistence:
    def __init__(self, authentication):
        self.database = authentication.database

    def initialize(self, trust_sha256):
        with self.database.transaction() as db:
            row = db.execute('SELECT * FROM nlp_dependency_authority WHERE singleton=1').fetchone()
            if row is None:
                # Schema sentinel distinguishes first use from lost authority.
                raise ValueError('nlp_authority_missing')
            value = json.loads(row['state_json'])
            require(hashlib.sha256(row['state_json'].encode()).hexdigest() == row['state_sha256'])
            require(value['trust_sha256'] in (None, trust_sha256), 'nlp_trust_changed')
            if value['trust_sha256'] is None:
                value['trust_sha256'] = trust_sha256
                self._save(db, value)
            return value

    @staticmethod
    def _save(db, state):
        raw = canonical(state)
        require(len(raw) <= 4 * 1024 * 1024)
        cursor = db.execute('UPDATE nlp_dependency_authority SET state_json=?,state_sha256=? WHERE singleton=1',
                            (raw.decode(), hashlib.sha256(raw).hexdigest()))
        require(cursor.rowcount == 1, 'nlp_authority_missing')

    def authority(self):
        with self.database.read() as db:
            row = db.execute('SELECT * FROM nlp_dependency_authority WHERE singleton=1').fetchone()
            require(row is not None, 'nlp_authority_missing')
            require(hashlib.sha256(row['state_json'].encode()).hexdigest() == row['state_sha256'])
            return json.loads(row['state_json'])

    def update(self, transform):
        with self.database.transaction() as db:
            row = db.execute('SELECT * FROM nlp_dependency_authority WHERE singleton=1').fetchone()
            require(row is not None, 'nlp_authority_missing')
            require(hashlib.sha256(row['state_json'].encode()).hexdigest() == row['state_sha256'])
            state = transform(json.loads(row['state_json']))
            self._save(db, state)
            return state

    def operations(self):
        with self.database.read() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT document FROM nlp_dependency_operations ORDER BY operation_id')]

    def operation(self, value):
        raw = canonical(value)
        require(len(raw) <= 1024 * 1024)
        with self.database.transaction() as db:
            db.execute("DELETE FROM nlp_dependency_operations WHERE operation_id IN "
                "(SELECT operation_id FROM nlp_dependency_operations WHERE "
                "json_extract(document,'$.state') IN ('complete','refused','cancelled') "
                "AND coalesce(json_extract(document,'$.in_flight'),0)=0 "
                "ORDER BY operation_id LIMIT max(0,(SELECT count(*) FROM nlp_dependency_operations)-30))")
            exists = db.execute('SELECT 1 FROM nlp_dependency_operations WHERE operation_id=?', (value['id'],)).fetchone()
            require(exists or db.execute('SELECT count(*) FROM nlp_dependency_operations').fetchone()[0] < 32,
                    'nlp_operation_limit')
            db.execute('INSERT INTO nlp_dependency_operations VALUES(?,?) ON CONFLICT(operation_id) '
                       'DO UPDATE SET document=excluded.document', (value['id'], raw.decode()))

    def install(self, profile, closure, *, planned=False):
        raw = canonical_bytes(profile)
        with self.database.transaction() as db:
            # Removed cache metadata is disposable; authority tombstones are separate.
            db.execute("DELETE FROM nlp_dependency_profiles WHERE state='removed' AND digest<>?",
                       (profile.identity_digest(),))
            exists = db.execute('SELECT 1 FROM nlp_dependency_profiles WHERE digest=?', (profile.identity_digest(),)).fetchone()
            require(exists or db.execute('SELECT count(*) FROM nlp_dependency_profiles').fetchone()[0] < MAX_PROFILES,
                    'nlp_profile_limit')
            state = 'planned' if planned else 'available'
            if planned:
                db.execute("INSERT INTO nlp_dependency_profiles VALUES(?,?,?,?) ON CONFLICT(digest) "
                           "DO UPDATE SET state=CASE WHEN state='removed' THEN 'planned' ELSE state END",
                           (profile.identity_digest(), raw, closure, state))
            else:
                db.execute("INSERT INTO nlp_dependency_profiles VALUES(?,?,?,?) ON CONFLICT(digest) "
                           "DO UPDATE SET state='available'", (profile.identity_digest(), raw, closure, state))

    def profiles(self):
        with self.database.read() as db:
            rows = db.execute('SELECT * FROM nlp_dependency_profiles').fetchall()
        values = []
        for row in rows:
            profile = read_profile(bytes(row['document']))
            require(profile.identity_digest() == row['digest'] and artifact_closure_digest(tuple(
                a for a in profile.artifacts if a.role in ('weights','tokenizer','configuration'))) == row['closure_digest'],
                'nlp_profile_storage')
            values.append((profile, row['closure_digest'], row['state']))
        return values

    def removed(self, digest):
        with self.database.transaction() as db:
            db.execute("UPDATE nlp_dependency_profiles SET state='removed' WHERE digest=?", (digest,))

    def removing(self, closure):
        with self.database.transaction() as db:
            db.execute("UPDATE nlp_dependency_profiles SET state='removing' WHERE closure_digest=?", (closure,))
