"""Named synthetic inputs and independent authority for exchange conformance."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

from app.analytics.exchange_codec import content_digest, facts_digest
from app.analytics.exchange_contracts import (
    ExchangeClassification, ExchangeConversation, ExchangeFacts,
    ExchangeObservation, ExchangeSourceBinding,
)
from app.analytics.identity import canonical_identity
from app.analytics.opaque_refs import account_ref, conversation_ref, message_ref
from app.analytics.query_contracts import QuestionEvidence, QuestionPlan, utc_instant
from app.canonical.read_models import AccountReadModel

FIXTURE_NAMES = ("no-later-reply", "pricing-discussions")
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests/fixtures/analytics/questions"


def load_cases(name: str) -> list[dict]:
    if name not in FIXTURE_NAMES:
        raise ValueError("exchange_fixture_not_allowed")
    document = json.loads((FIXTURE_ROOT / (name + ".json")).read_bytes())
    if document.get("schema") != "analytics-question-cases.v1" or document.get("synthetic") is not True:
        raise ValueError("exchange_fixture_invalid")
    return document["cases"]


def source_version(message: dict, *, version=None) -> str:
    value = {key: message[key] for key in (
        "account", "conversation", "id", "at", "role", "kind", "order", "ordering", "deleted", "version")}
    if version is not None:
        value["version"] = version
    return content_digest(value)


class SyntheticSource:
    """Rebuild source and fact authority retained independently of the exchange."""

    def __init__(self, case: dict):
        self.case = deepcopy(case)
        self.account = case["account"]
        if not self.account.startswith("synthetic-"):
            raise ValueError("exchange_fixture_invalid")
        self.now = utc_instant(case["now"])
        self.gateway = None

    def attach_canonical(self, directory):
        from app.analytics.canonical_source import HistoryAnalyticsSource
        from app.persistence.factory import create_canonical_repositories

        repositories = create_canonical_repositories("sqlite", canonical_path=directory / "canonical.sqlite3",
                                                     projection_path=directory / "bridge.sqlite3")
        with repositories.database.transaction() as db:
            exists = db.execute("SELECT 1 FROM account_heads WHERE creator_account_id=?", (self.account,)).fetchone()
            if exists is None:
                db.execute("INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,?,?)",
                           (self.account, self.case["source_revision"], self.now.isoformat()))
                canonical = self.account_read_model(self.account)
                for chat, value in canonical.conversations.items():
                    db.execute("""INSERT INTO account_chats(creator_account_id,chat_id,record_kind,
                        platform_user_id,display_name,content_hash,winning_stream_epoch,winning_source_seq,
                        is_deleted,updated_at) VALUES (?,?,'full',?,NULL,?,1,1,0,?)""",
                        (self.account, chat, value["platform_user_id"], content_digest(value), self.now.isoformat()))
                    for message in value["messages"]:
                        db.execute("""INSERT INTO account_messages(creator_account_id,message_id,chat_id,
                            sender_platform_user_id,text,sent_at,direction,content_hash,winning_stream_epoch,
                            winning_source_seq,is_deleted,updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,0,?)""",
                            (self.account, message["message_id"], chat,
                             self.account if message["direction"] == "outbound" else value["platform_user_id"],
                             message["text"], utc_instant(message["sent_at"]).isoformat(), message["direction"],
                             content_digest(message), message["source_ordinal"], self.now.isoformat()))
        self.gateway = HistoryAnalyticsSource(repositories.history)
        return repositories

    def account_exists(self, account):
        return account == self.account

    def account_revisions(self):
        return [(self.account, self.case["source_revision"])]

    def account_read_model(self, account):
        if not self.account_exists(account):
            raise ValueError("exchange_account_invalid")
        if self.gateway is not None:
            return self.gateway.account_read_model(account)
        grouped = defaultdict(list)
        for item in self.case["messages"]:
            if item["account"] == account and not item["deleted"]:
                grouped[item["conversation"]].append(item)
        conversations = {}
        for chat, messages in sorted(grouped.items()):
            messages.sort(key=lambda m: (utc_instant(m["at"]), m["id"]))
            conversations[chat] = {
                "conversation_id": chat, "platform_user_id": "synthetic-participant-" + chat,
                "messages": [{"message_id": m["id"], "source_ordinal": i,
                    "text": "Synthetic observation " + str(m["version"]),
                    "sent_at": m["at"], "direction": "outbound" if m["role"] == "creator" else "inbound"}
                    for i, m in enumerate(messages)],
            }
        return AccountReadModel(view_revision=self.case["source_revision"], conversations=conversations)

    def read_identity(self, account):
        if self.gateway is not None:
            return self.gateway.read_identity(account)
        return canonical_identity(self.account_read_model(account)) if self.account_exists(account) else None

    def facts(self):
        observations, conversations = [], set()
        cutoff = self.now - timedelta(days=90)
        for item in self.case["messages"]:
            if item["account"] != self.account or item["deleted"] or utc_instant(item["at"]) <= cutoff:
                continue
            chat = conversation_ref(self.account, item["conversation"])
            conversations.add(chat)
            observations.append(ExchangeObservation(
                evidence=QuestionEvidence(account_ref=account_ref(self.account), conversation_ref=chat,
                    message_ref=message_ref(self.account, item["conversation"], item["id"]),
                    source_revision=self.case["source_revision"], source_version_digest=source_version(item),
                    sent_at=item["at"]), role=item["role"], kind=item["kind"], order=item["order"],
                ordering=item["ordering"], classification=ExchangeClassification(status=item["pricing"],
                    input_version_digest=(source_version(item, version=item["finding_version"])
                        if item["finding_version"] is not None else None), language=item["language"],
                    configuration_digest=content_digest({"fixture": self.case["id"], "method": "declared-fixture.v1"}))))
        return ExchangeFacts(conversations=tuple(ExchangeConversation(conversation_ref=ref,
            coverage=self.case["coverage"]) for ref in sorted(conversations)),
            observations=tuple(sorted(observations, key=lambda item: item.message_ref)))

    def binding(self):
        identity = self.read_identity(self.account)
        return ExchangeSourceBinding(account_ref=account_ref(self.account), source_revision=identity.revision,
            canonical_content_digest=identity.content_digest, source_facts_digest=facts_digest(self.facts()))

    def plan(self):
        return QuestionPlan(question=self.case["question"], start=self.case["start"], end=self.case["end"],
                            cutoff=self.case["cutoff"], timezone="UTC")

    def assert_expected(self, page):
        expected = self.case["expected"]
        matches = {conversation_ref(self.account, c) for c in expected["matches"]}
        evidence = {conversation_ref(self.account, c): {message_ref(self.account, c, m) for m in refs}
                    for c, refs in expected["evidence"].items()}
        if ({row.conversation_ref for row in page.rows} != matches
                or page.undetermined_conversation_count != len(expected["undetermined"])
                or {row.conversation_ref: {e.message_ref for e in row.evidence} for row in page.rows} != evidence):
            raise ValueError("exchange_question_mismatch")
