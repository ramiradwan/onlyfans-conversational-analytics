"""Adopted history kinds in conversation graphs and activity metrics."""

import json
from datetime import timedelta

import pytest

from app.analytics.graph_projection import stable_node_id
from app.analytics.opaque_refs import account_ref, message_ref
from app.models.analytics import GraphNodeKind, GraphRelation
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message, make_fixture,
)
from tests.test_history_kind_adoption import synthetic_kind
from tests.test_tied_insertion_safety import small_fixture

pytestmark = pytest.mark.ci_tier('integration')


def set_kind(db, chat, message, kind):
    db.execute('UPDATE account_messages SET event_kind_json=? '
               'WHERE creator_account_id=? AND chat_id=? AND message_id=?',
               (json.dumps(kind), ACCOUNT, chat, message))


def message_node(chat, message):
    return stable_node_id(account_ref(ACCOUNT), GraphNodeKind.MESSAGE, message_ref(ACCOUNT, chat, message))


def test_kind_only_correction_replaces_speaker_entity_and_turns(tmp_path):
    f = make_fixture(tmp_path, conversations=2, messages=3)
    chat, target = 'chat-0', 'm-0-1'
    node_id = message_node(chat, target)
    try:
        with f.repositories.database.transaction() as db:
            db.execute("UPDATE account_messages SET text='The price is $20.' WHERE message_id=?", (target,))
            set_kind(db, chat, target, synthetic_kind())
        before = f.pipeline.project_account(ACCOUNT).artifact
        assert before.projection.creator_metrics.entity_counts['amount'] == 1
        assert any(e.target_id == node_id and e.relation == GraphRelation.SENT for e in before.edges)
        assert any(e.source_id == node_id and e.relation == GraphRelation.MENTIONS_ENTITY for e in before.edges)

        with f.repositories.database.transaction() as db:
            set_kind(db, chat, target, synthetic_kind('non_message_event'))
            advance(db)
        corrected = f.pipeline.project_account(ACCOUNT).artifact
        cold_equal(f, corrected)
        assert all(n.node_id != node_id for n in corrected.nodes)
        assert all(node_id not in (e.source_id, e.target_id) for e in corrected.edges)
        assert any(e.relation == GraphRelation.PRECEDES and
                   (e.source_id, e.target_id) == (message_node(chat, 'm-0-0'), message_node(chat, 'm-0-2'))
                   for e in corrected.edges)
        metrics = corrected.projection.creator_metrics
        assert 'amount' not in metrics.entity_counts
        assert (metrics.message_count, metrics.outbound_message_count) == (5, 1)
        assert sorted((m.message_count, m.turn_count, m.responded_count)
                      for m in corrected.projection.conversation_metrics) == [(2, 1, 0), (3, 3, 1)]
        assert len(f.source.account_read_model(ACCOUNT).conversations[chat]['messages']) == 3
        assert not f.pipeline.project_account(ACCOUNT).changed

        with f.repositories.database.transaction() as db:
            set_kind(db, chat, target, dict(synthetic_kind(), mapping_version='unknown'))
            advance(db)
        restored = f.pipeline.project_account(ACCOUNT).artifact
        assert restored.nodes == before.nodes and restored.edges == before.edges
        cold_equal(f, restored)
    finally:
        cleanup(f)


def test_correction_of_only_message_matches_empty_cold_graph(tmp_path):
    f = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.repositories.database.transaction() as db:
            set_kind(db, 'chat-0', 'm-0-0', synthetic_kind('non_message_event'))
            advance(db)
        corrected = f.pipeline.project_account(ACCOUNT).artifact
        cold_equal(f, corrected)
        assert corrected.projection.creator_metrics.participant_count == 0
        assert corrected.projection.creator_metrics.message_count == 0
        assert not corrected.projection.conversation_metrics
        assert len(corrected.nodes) == 1 and corrected.nodes[0].properties == {'role': 'creator'}
        assert not corrected.edges
    finally:
        cleanup(f)


@pytest.mark.parametrize('tied', [False, True], ids=['append', 'tied-insertion'])
def test_new_notice_cannot_enter_reused_conversation(tmp_path, monkeypatch, tied):
    monkeypatch.setattr('app.analytics.conversation_append.MIN_APPEND_MESSAGES', 2)
    f = small_fixture(tmp_path)
    try:
        before = f.pipeline.project_account(ACCOUNT).artifact
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'tie-05', NOW-timedelta(microseconds=1) if tied else NOW,
                           2, text='The price is $20.')
            set_kind(db, 'chat-1', 'tie-05', synthetic_kind('non_message_event', 'chat-1'))
            advance(db)
        after = f.pipeline.project_account(ACCOUNT).artifact
        cold_equal(f, after)
        assert after.projection.creator_metrics == before.projection.creator_metrics
        assert [n.node_id for n in after.nodes] == [n.node_id for n in before.nodes]
        assert after.edges == before.edges
        if tied:
            ordinals = [n.properties['source_ordinal'] for n in after.nodes
                        if n.node_id == message_node('chat-1', 'tie-06')]
            assert ordinals == [4]
    finally:
        cleanup(f)
