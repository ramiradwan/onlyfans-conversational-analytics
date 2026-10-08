"""Check adapter publication and budgets without claiming cloud compatibility."""

from copy import deepcopy
import json

import pytest

from app.analytics.exchange_codec import ExchangeInvalid, canonical_bytes, encode_exchange
from app.analytics.query_execution import QuestionResultInvalid
from tests.test_analytics_exchange import make_bundle
from tools.analytics_exchange_cosmos import CosmosExchangeTarget
from tools.analytics_exchange_fixtures import load_cases
from tools.analytics_exchange_local import execute_question, ExchangeFactsSession
from tools.analytics_exchange_records import instant_us

pytestmark = [pytest.mark.ci_tier("fast")]


class FixtureTransport:
    def __init__(self):
        self.records, self.manifest, self.calls = {}, None, []
        self.alter = False

    def request(self, op, scope=None, *, timeout=None, **fields):
        self.calls.append((op, timeout))
        result = None
        if op in ("put_vertex", "put_edge"):
            old = self.records.setdefault(fields["logical_id"], deepcopy(fields))
            if old != fields:
                raise ExchangeInvalid("analytics_exchange_identity_conflict")
        elif op == "publish":
            if self.manifest is not None and self.manifest != fields["manifest_json"]:
                raise ExchangeInvalid("analytics_exchange_identity_conflict")
            self.manifest = fields["manifest_json"]
        elif op == "read_manifest":
            result = self.manifest
        elif op in ("read_records", "question_facts"):
            records = sorted(self.records.values(), key=lambda item: item["logical_id"])
            if op == "question_facts":
                records = [item for item in records if item["label"] == "exchange_observation"
                    and fields["retention_us"] < item["fields"]["sent_us"] <= fields["cutoff_us"]
                    and (fields["conversation_ref"] is None or item["fields"]["conversation_ref"] == fields["conversation_ref"])
                    and (fields["question"] != "pricing_discussions.v1" or fields["start_us"] <= item["fields"]["sent_us"] < fields["end_us"])]
            result = deepcopy([item for item in records if item["logical_id"] > fields["after"]][:fields["limit"]])
            if self.alter and op == "question_facts" and result:
                value = json.loads(result[0]["record_json"])
                value["kind"] = "system" if value["kind"] != "system" else "unknown"
                result[0]["record_json"] = json.dumps(value)
        elif op == "cleanup":
            self.records.clear()
            self.manifest = None
        else:
            raise AssertionError(op)
        return {"result": result, "metrics": {"request_charge": 1, "retries": 0}}


@pytest.mark.parametrize("case", load_cases("no-later-reply") + load_cases("pricing-discussions"),
                         ids=lambda case: case["id"])
def test_bounded_remote_fact_selection_preserves_shared_question_answers(case):
    source, bundle = make_bundle(case)
    transport = FixtureTransport()
    remote = CosmosExchangeTarget(transport, source)
    raw = encode_exchange(bundle)
    remote.import_bytes(raw)
    assert remote.export() == raw
    transport.calls.clear()
    page = execute_question(remote, source).page
    source.assert_expected(page)
    assert all(op in {"question_facts", "read_manifest"} for op, _ in transport.calls)
    assert all(timeout is not None and timeout > 0 for _, timeout in transport.calls)


@pytest.mark.parametrize("checkpoint", ["vertices", "records", "validated", "published"])
def test_interruption_retains_one_immutable_generation(checkpoint):
    source, bundle = make_bundle()
    transport = FixtureTransport()
    remote = CosmosExchangeTarget(transport, source)
    raw = encode_exchange(bundle)
    def interrupt(stage):
        if stage == checkpoint:
            raise RuntimeError("synthetic interruption")
    with pytest.raises(RuntimeError):
        remote.import_bytes(raw, fault=interrupt)
    assert (transport.manifest is not None) == (checkpoint == "published")
    with pytest.raises(ExchangeInvalid, match="not_published"):
        remote.export()
    remote.import_bytes(raw)
    count = len(transport.records)
    remote.import_bytes(raw)
    assert len(transport.records) == count
    assert remote.export() == raw
    remote.cleanup()
    assert not transport.records and transport.manifest is None


def test_fact_tampering_and_stale_source_are_rejected_before_a_result():
    source, bundle = make_bundle()
    transport = FixtureTransport()
    remote = CosmosExchangeTarget(transport, source)
    remote.import_bytes(encode_exchange(bundle))
    transport.alter = True
    with pytest.raises(QuestionResultInvalid):
        execute_question(remote, source)
    transport.alter = False
    source.case["messages"][0]["deleted"] = True
    transport.calls.clear()
    with pytest.raises(QuestionResultInvalid):
        execute_question(remote, source)
    assert not transport.calls


def test_expiry_during_publication_never_creates_ready_marker():
    source, bundle = make_bundle()
    transport = FixtureTransport()
    remote = CosmosExchangeTarget(transport, source)
    def expire(stage):
        if stage == "validated":
            source.now = bundle.snapshot.retention_due_at
    with pytest.raises(ExchangeInvalid):
        remote.import_bytes(encode_exchange(bundle), fault=expire)
    assert transport.manifest is None


def test_missing_published_observation_cannot_create_a_no_reply_result():
    source, bundle = make_bundle()
    transport = FixtureTransport()
    remote = CosmosExchangeTarget(transport, source)
    remote.import_bytes(encode_exchange(bundle))
    observation = next(key for key in transport.records if key.startswith("observation:"))
    del transport.records[observation]
    assert transport.manifest is not None
    with pytest.raises(QuestionResultInvalid):
        execute_question(remote, source)
