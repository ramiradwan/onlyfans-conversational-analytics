"""Recurrence is a count of verified incoming senders over one saved publication."""
from dataclasses import replace
from datetime import timedelta

import pytest

from app.analytics.errors import ProjectionUnavailable
from app.analytics.evidence import EvidenceUnavailable
from app.models.recurring_mentions import RecurringMentionsRequest
from tests.continuous_analytics_fixture import ACCOUNT, advance
from tests.test_nlp_read_projection import api, ready  # noqa: F401

pytestmark = pytest.mark.ci_tier("integration")


def _request(projection, **params):
    return RecurringMentionsRequest(expected_generation=projection.projection_generation,
        expected_projection_digest=projection.projection_digest, **params)


def _add_second_sender(ready):
    with ready.fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_chats SET display_name='Shared name' WHERE creator_account_id=?", (ACCOUNT,))
        db.execute("UPDATE account_chats SET platform_user_id='counterpart-two' "
                   "WHERE creator_account_id=? AND chat_id='chat-1'", (ACCOUNT,))
        db.execute("UPDATE account_messages SET sender_platform_user_id='counterpart-two', text=?, "
                   "content_hash='new-hash', winning_source_seq=9 WHERE creator_account_id=? AND message_id='m-1-0'",
                   ("😀 echo echo case0", ACCOUNT))
        advance(db)
    for _ in range(10):
        if not ready.runner.process_one(ACCOUNT):
            break
    result = ready.runner.publish_pending(ACCOUNT)
    assert result.changed
    ready.fixture.source.prepare_question_identity(ACCOUNT)
    return result.artifact.projection


def test_repeated_spans_and_separate_chats_require_distinct_incoming_senders(ready):
    request = _request(ready.projection)
    empty = ready.resources.literal_findings(ready.policy, request)
    assert empty.availability == "available"
    assert empty.coverage.eligible_messages == 4
    assert empty.row_page.total == empty.evidence_page.total == 0

    projection = _add_second_sender(ready)
    result = ready.resources.literal_findings(ready.policy, _request(projection))
    assert result.schema_version == "analytics-recurring-mentions.v1"
    assert result.requested_window.end - result.requested_window.start == timedelta(days=90)
    assert result.coverage.eligible_messages == 3  # The other incoming sender in chat-1 is unknown.
    assert result.coverage.partial_messages == 1
    row, = result.row_page.items
    assert (row.literal_key, row.supporting_senders, row.supporting_messages, row.occurrences) == ("echo", 2, 3, 5)
    assert {entry.phrase_id for entry in result.evidence_page.items} == {row.phrase_id}
    assert len({entry.reference.message_ref for entry in result.evidence_page.items}) == 3
    for entry in result.evidence_page.items:
        exact = ready.resources.resolve(ready.policy, entry.reference)
        span = entry.reference.span
        assert exact.text[span.start:span.end] == row.literal_key

    selected = _request(projection, phrase_id=row.phrase_id, expected_normalization_sha256=result.normalization_sha256,
                        evidence_offset=2, evidence_limit=2)
    page = ready.resources.literal_findings(ready.policy, selected)
    assert page.row_page == result.row_page and page.evidence_page.items == result.evidence_page.items[2:4]
    assert page.evidence_page.has_more
    with pytest.raises(ProjectionUnavailable):
        ready.resources.literal_findings(ready.policy, request)


def test_report_http_entry_rejects_extra_scope_and_reopens_without_inference(api, ready, monkeypatch):
    client, scheduler = api
    def unexpected_inference(*args, **kwargs):
        pytest.fail("A saved report read attempted inference.")
    monkeypatch.setattr(ready.runner, 'process_one', unexpected_inference)
    query = _request(ready.projection).model_dump(mode="json", exclude_none=True)
    route = "/api/v1/insights/findings/recurring-mentions"
    first = client.get(route, params=query)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert client.get(route, params=query).json() == first.json()
    for invalid in ({"direction": "outbound"}, {"phrase_id": "sha256:" + "0" * 64}, {"row_offset": -1}):
        refused = client.get(route, params={**query, **invalid})
        assert refused.status_code == 422
        assert refused.headers["cache-control"] == "no-store"
    assert client.get(route, params={**query, "expected_generation": 900}).status_code == 503
    scheduler.request_recovery.assert_not_awaited()


def test_removing_a_sender_retires_the_old_evidence_and_the_recurrence(ready):
    from app.persistence.retention import CreatorVaultRetention

    projection = _add_second_sender(ready)
    request = _request(projection)
    report = ready.resources.literal_findings(ready.policy, request)
    quotes = [ready.resources.resolve(ready.policy, hit.reference) for hit in report.evidence_page.items]
    removed = next(quote.reference for quote in quotes if quote.location.conversation_id == "chat-1")
    other_account = replace(ready.policy, identity=replace(ready.policy.identity, creator_account_id="other-account"))
    with pytest.raises(EvidenceUnavailable):
        ready.resources.resolve(other_account, removed)

    retention = CreatorVaultRetention(ready.fixture.repositories.database, clock=lambda: ready.fixture.clock.now)
    assert retention.delete_participant(ACCOUNT, "counterpart-two") > 0
    with pytest.raises(EvidenceUnavailable):
        ready.resources.resolve(ready.policy, removed)
    with pytest.raises(ProjectionUnavailable):
        ready.resources.literal_findings(ready.policy, request)

    ready.runner.reconcile(ACCOUNT)
    while ready.runner.process_one(ACCOUNT):
        pass
    current = ready.runner.publish_pending(ACCOUNT)
    assert current.changed
    ready.fixture.source.prepare_question_identity(ACCOUNT)
    after = ready.resources.literal_findings(ready.policy, _request(current.artifact.projection))
    assert after.availability == "available" and after.row_page.total == after.evidence_page.total == 0
