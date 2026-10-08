"""Compatibility snapshots use retained analytics without deleting history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.errors import CanonicalAccountNotFound
from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.runtime import AnalyticsRuntime
from app.analytics.scheduling import InProcessProjectionScheduler
from app.models.analytics import WindowScope
from app.persistence.factory import CanonicalRepositories, create_canonical_repositories
from app.services import insights_service


pytestmark = [pytest.mark.ci_tier("integration")]

NOW = datetime(2026, 10, 8, 16, 24, tzinfo=timezone.utc)
ACCOUNT = "snapshot-account"
MessageInput = tuple[str, datetime, str, str]


def _seed_account(
    repositories: CanonicalRepositories,
    account_id: str,
    conversations: dict[str, list[MessageInput]],
) -> None:
    """Keep expired and current messages in the real canonical repository."""
    with repositories.database.transaction() as connection:
        connection.execute(
            """INSERT INTO account_heads(
                   creator_account_id,canonical_revision,updated_at) VALUES (?,1,?)""",
            (account_id, NOW.isoformat()),
        )
        for chat_id, messages in conversations.items():
            connection.execute(
                """INSERT INTO account_chats(
                       creator_account_id,chat_id,record_kind,platform_user_id,
                       display_name,upstream_updated_at,content_hash,
                       winning_stream_epoch,winning_source_seq,winning_event_id,
                       is_deleted,updated_at)
                   VALUES (?,?,'full',?,?,?,'chat-hash',1,1,'chat-event',0,?)""",
                (
                    account_id,
                    chat_id,
                    f"participant-{account_id}-{chat_id}",
                    f"Participant {account_id}",
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
            for index, (message_id, sent_at, text, direction) in enumerate(messages, 1):
                connection.execute(
                    """INSERT INTO account_messages(
                           creator_account_id,message_id,chat_id,
                           sender_platform_user_id,text,sent_at,direction,
                           upstream_updated_at,content_hash,winning_stream_epoch,
                           winning_source_seq,winning_event_id,is_deleted,updated_at)
                       VALUES (?,?,?,?,?,?,?,NULL,?,1,?,?,0,?)""",
                    (
                        account_id,
                        message_id,
                        chat_id,
                        f"participant-{account_id}-{chat_id}",
                        text,
                        sent_at.isoformat(),
                        direction,
                        f"hash-{message_id}",
                        index,
                        f"event-{message_id}",
                        NOW.isoformat(),
                    ),
                )


def _runtime(
    repositories: CanonicalRepositories,
    monkeypatch: pytest.MonkeyPatch,
) -> AnalyticsRuntime:
    source = HistoryAnalyticsSource(repositories.history)
    pipeline = AnalyticsPipeline(source, clock=lambda: NOW)
    runtime = AnalyticsRuntime(
        source=source,
        pipeline=pipeline,
        scheduler=InProcessProjectionScheduler(pipeline),
    )
    monkeypatch.setattr(insights_service, "analytics_runtime", lambda source=None: runtime)
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["expired", "mixed", "empty_chat", "empty_account"])
async def test_snapshot_uses_only_current_projection_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    expired = [
        ("older", NOW - timedelta(days=91), "expired private marker", "inbound"),
        ("boundary", NOW - timedelta(days=90), "boundary private marker", "outbound"),
    ]
    recent = [
        ("recent-in", NOW - timedelta(days=1), "Please help with pricing", "inbound"),
        (
            "recent-out",
            NOW - timedelta(days=1) + timedelta(minutes=5),
            "Thanks, here is the pricing information",
            "outbound",
        ),
    ]
    conversations = {
        "expired": {"history-only": expired},
        "mixed": {
            "partially-retained": expired + recent,
            "history-only": [
                ("other-old", NOW - timedelta(days=91), "other expired marker", "inbound")
            ],
            "empty": [],
        },
        "empty_chat": {"empty": []},
        "empty_account": {},
    }[state]
    expected_ids = ["recent-in", "recent-out"] if state == "mixed" else []
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=tmp_path / "canonical.sqlite3"
    )
    _seed_account(repositories, ACCOUNT, conversations)
    runtime = _runtime(repositories, monkeypatch)
    try:
        canonical_before = runtime.source.account_read_model(ACCOUNT)
        projection = runtime.pipeline.project_account(ACCOUNT).artifact.projection
        snapshot = await insights_service.get_full_snapshot(ACCOUNT, source=runtime.source)
        compatibility = await insights_service.fetch_conversations_for_account(
            ACCOUNT, source=runtime.source
        )

        assert compatibility == snapshot.conversations
        assert [message.id for chat in snapshot.conversations for message in chat.messages] == expected_ids
        assert [chat.analyticsRef for chat in snapshot.conversations] == [
            item.conversation_ref for item in projection.conversation_metrics
        ]
        assert snapshot.analytics.creator_metrics.message_count == len(expected_ids)
        assert snapshot.analytics.message_enrichments == projection.message_enrichments
        assert snapshot.analytics.conversation_metrics == projection.conversation_metrics
        assert snapshot.account_ref == snapshot.analytics.account_ref == account_ref(ACCOUNT)
        assert snapshot.conversation_window == projection.window
        assert snapshot.conversation_window.scope is WindowScope.ALL_TIME
        provenance = snapshot.conversation_range_provenance
        assert provenance.source_revision == canonical_before.view_revision
        assert provenance.projection_generation == projection.projection_generation
        assert provenance.projection_digest == projection.content_digest
        assert provenance.canonical_content_digest == projection.canonical_content_digest
        assert provenance.graph_digest == projection.graph_digest
        assert provenance.sample_count == provenance.eligible_sample_count == len(expected_ids)
        assert snapshot.conversation_metric_provenance.sample_count == len(expected_ids)
        if expected_ids:
            chat = snapshot.conversations[0]
            assert chat.conversationId == "partially-retained"
            assert chat.messageCount == 2
            assert chat.startDate == recent[0][1]
            assert chat.endDate == recent[1][1]
            assert chat.averageResponseTime == 5
            assert provenance.sample_coverage == 1.0
            assert provenance.unavailable_reason is None
        else:
            assert snapshot.conversations == []
            assert snapshot.analytics.creator_metrics.conversation_count == 0
            assert provenance.effective_window.start is None
            assert provenance.effective_window.end is None
            assert provenance.sample_coverage is None
            assert provenance.unavailable_reason == "no_eligible_samples"

        serialized = snapshot.model_dump_json()
        for chat_id, messages in conversations.items():
            for message_id, _, text, _ in messages:
                if message_id not in expected_ids:
                    assert text not in serialized
                    assert message_ref(ACCOUNT, chat_id, message_id) not in serialized
        assert runtime.source.account_read_model(ACCOUNT) == canonical_before
        with pytest.raises(CanonicalAccountNotFound):
            await insights_service.get_full_snapshot("unknown-account", source=runtime.source)
    finally:
        assert await runtime.scheduler.close(timeout=1)


@pytest.mark.asyncio
async def test_retained_snapshots_do_not_mix_accounts_with_identical_local_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = create_canonical_repositories(
        "sqlite", canonical_path=tmp_path / "canonical.sqlite3"
    )
    for account_id in ("account-a", "account-b"):
        _seed_account(
            repositories,
            account_id,
            {
                "shared-chat": [
                    ("shared-message", NOW - timedelta(days=1), f"Private {account_id}", "inbound"),
                    ("expired-message", NOW - timedelta(days=91), f"Expired {account_id}", "inbound"),
                ]
            },
        )
    runtime = _runtime(repositories, monkeypatch)
    try:
        for account_id in ("account-a", "account-b"):
            runtime.pipeline.project_account(account_id)
            snapshot = await insights_service.get_full_snapshot(account_id, source=runtime.source)
            assert snapshot.account_ref == account_ref(account_id)
            assert len(snapshot.conversations) == 1
            chat = snapshot.conversations[0]
            assert chat.analyticsRef == conversation_ref(account_id, "shared-chat")
            assert [message.text for message in chat.messages] == [f"Private {account_id}"]
            assert chat.withUser.id == f"participant-{account_id}-shared-chat"
            other_account = "account-b" if account_id == "account-a" else "account-a"
            assert f"Private {other_account}" not in snapshot.model_dump_json()
            assert f"Expired {account_id}" not in snapshot.model_dump_json()
            assert message_ref(account_id, "shared-chat", "expired-message") not in snapshot.model_dump_json()
    finally:
        assert await runtime.scheduler.close(timeout=1)
