"""Synthetic canonical conversations for incremental analytics checks."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.enrichment import EnrichmentStage
from app.analytics.factory import create_analytics_stores
from app.analytics.pipeline import AnalyticsPipeline
from app.persistence.factory import create_canonical_repositories
from tests.test_enrichment_reuse import counters

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
ACCOUNT = 'synthetic-continuous-owner'


class RecordingSource(HistoryAnalyticsSource):
    def __init__(self, history):
        super().__init__(history)
        self.loaded = []
        self.full_reads = 0

    def conversation_read_model(self, account_id, conversation_id, **kwargs):
        self.loaded.append(conversation_id)
        return super().conversation_read_model(account_id, conversation_id, **kwargs)

    def account_read_model(self, account_id):
        self.full_reads += 1
        return super().account_read_model(account_id)


def make_fixture(tmp_path, backend='sqlite', *, conversations=3, messages=3):
    repositories = create_canonical_repositories('sqlite', canonical_path=tmp_path/'canonical.sqlite3')
    with repositories.database.transaction() as db:
        db.execute('INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,?,?)',
                   (ACCOUNT, 1, NOW.isoformat()))
        for chat in range(conversations):
            db.execute("""INSERT INTO account_chats(creator_account_id,chat_id,record_kind,
                platform_user_id,display_name,content_hash,winning_stream_epoch,winning_source_seq,
                is_deleted,updated_at) VALUES (?,?,'full','synthetic-fan',NULL,'chat-hash',1,1,0,?)""",
                (ACCOUNT, f'chat-{chat}', NOW.isoformat()))
            for message in range(messages):
                insert_message(db, f'chat-{chat}', f'm-{chat}-{message}',
                    NOW-timedelta(days=8)+timedelta(hours=chat, minutes=message), message)
    source = RecordingSource(repositories.history)
    clock = SimpleNamespace(now=NOW)
    stores = create_analytics_stores(backend, projections_path=tmp_path/'analytics.sqlite3',
        activation=repositories.projection_activation, canonical_identity_reader=source.read_identity,
        retention_clock=lambda: clock.now)
    analyzers = counters()
    stage = EnrichmentStage(sentiment=analyzers[0], topics_entities=analyzers[1], engagement=analyzers[2])
    pipeline = AnalyticsPipeline(source, projections=stores.projections, enrichment=stage, clock=lambda: clock.now)
    return SimpleNamespace(repositories=repositories, source=source, clock=clock, stores=stores,
        pipeline=pipeline, analyzers=analyzers)


def insert_message(db, chat, message_id, sent_at, sequence=0, *, text='Thanks pricing'):
    db.execute("""INSERT INTO account_messages(creator_account_id,message_id,chat_id,
        sender_platform_user_id,text,sent_at,direction,content_hash,winning_stream_epoch,
        winning_source_seq,is_deleted,updated_at) VALUES (?,?,?,'synthetic-fan',?,?,?,'message-hash',1,?,0,?)""",
        (ACCOUNT, message_id, chat, text, sent_at.isoformat(),
         'inbound' if sequence % 2 == 0 else 'outbound', sequence, NOW.isoformat()))


def advance(db):
    db.execute('UPDATE account_heads SET canonical_revision=canonical_revision+1 WHERE creator_account_id=?', (ACCOUNT,))


def cleanup(fixture):
    closer = getattr(fixture.stores.projections, 'close_retention_scheduler', None)
    if closer:
        closer()


def cold_equal(fixture, artifact):
    cold = AnalyticsPipeline(fixture.source, clock=lambda: fixture.clock.now,
        reuse_enrichment=False, reuse_conversations=False)
    raw = HistoryAnalyticsSource(fixture.repositories.history).account_read_model(ACCOUNT)
    expected = cold._build(ACCOUNT, raw, projection_generation=artifact.projection.projection_generation)
    assert artifact == expected
