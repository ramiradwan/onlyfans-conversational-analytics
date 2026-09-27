"""Deterministic source diagnostics. These fixtures are never licensed ingestion."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import time
from types import SimpleNamespace

from tools import analytics_qualification as q


class Journal:
    def __init__(self, directory: Path, process: str):
        self.directory, self.process, self.index = directory, process, 0

    def save(self, label: str, value: dict):
        self.index += 1
        q.write_once(self.directory / f"{self.index:05}-{label}.json",
                     {"process_instance": self.process, "observed": q.stamp(), "value": value})


def layout(size: int, index: int):
    return (0, index) if index < size // 2 else (1 + (index - size // 2) % 100, (index - size // 2) // 100)


def question_plan(manifest: dict, case: str) -> dict:
    from app.analytics.opaque_refs import conversation_ref
    ACCOUNT = "synthetic-continuous-owner"
    clock = datetime.fromisoformat(manifest["fixture"]["evaluation_clock"])
    return {"question": "no_later_creator_reply.v1", "timezone": "UTC", "page_size": 50,
            "start": (clock - timedelta(hours=48)).isoformat(), "end": clock.isoformat(),
            "cutoff": clock.isoformat(), "filters": {
                "conversation_ref": conversation_ref(ACCOUNT, "chat-0")} if case == "empty" else {}}


class Workload:
    def __init__(self, directory: Path, manifest: dict, size: int, *, reopen=False,
                 question_case: str | None = None, known_kinds=False):
        from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, insert_message
        from app.analytics.canonical_source import HistoryAnalyticsSource
        from app.analytics.enrichment import EnrichmentStage
        from app.analytics.factory import create_analytics_stores
        from app.analytics.pipeline import AnalyticsPipeline
        from app.persistence.factory import create_canonical_repositories
        from tests.test_enrichment_reuse import counters
        self.account, self.manifest, self.size = ACCOUNT, manifest, size
        self.clock = datetime.fromisoformat(manifest["fixture"]["evaluation_clock"])
        if reopen:
            repositories = create_canonical_repositories("sqlite", canonical_path=directory / "canonical.sqlite3")
            source = HistoryAnalyticsSource(repositories.history)
            stores = create_analytics_stores("sqlite", projections_path=directory / "analytics.sqlite3",
                activation=repositories.projection_activation, canonical_identity_reader=source.read_identity,
                retention_clock=lambda: self.clock, lazy=True)
            analyzers = counters()
            stage = EnrichmentStage(sentiment=analyzers[0], topics_entities=analyzers[1], engagement=analyzers[2])
            pipeline = AnalyticsPipeline(source, projections=stores.projections, graph=stores.graph,
                                         enrichment=stage, clock=lambda: self.clock)
            self.f = SimpleNamespace(repositories=repositories, source=source, stores=stores,
                                     pipeline=pipeline, analyzers=analyzers, clock=SimpleNamespace(now=self.clock))
        else:
            self.f = make_fixture(directory, conversations=101, messages=0)
            self.f.clock.now = self.clock
            with self.f.repositories.database.transaction() as db:
                for index in range(size):
                    chat, local = layout(size, index)
                    at = self.clock - timedelta(hours=48) + timedelta(seconds=index * 48 * 3600 / size)
                    insert_message(db, f"chat-{chat}", f"matrix-input-{index}", at, index)
                    if question_case:
                        count = size // 2 if chat == 0 else (size - size // 2 - chat) // 100 + 1
                        # Alternate within each conversation. Small threads end with a participant.
                        inbound = (local % 2 == (count - 1) % 2) if chat else local % 2 == 0
                        db.execute("UPDATE account_messages SET direction=? WHERE creator_account_id=? AND message_id=?",
                                   ("inbound" if inbound else "outbound", ACCOUNT, f"matrix-input-{index}"))
                if question_case == "tied_time":
                    last = size // 2 + ((size - size // 2 - 1) // 100) * 100
                    db.execute("UPDATE account_messages SET sent_at=(SELECT sent_at FROM account_messages WHERE creator_account_id=? AND message_id=?) WHERE creator_account_id=? AND message_id=?",
                               (ACCOUNT, f"matrix-input-{last}", ACCOUNT, f"matrix-input-{last - 100}"))
        self.last = None
        self.events, self.cleaned = [], {}
        original = self.f.pipeline.publish_candidate
        def observed(candidate):
            result = original(candidate)
            self.last = result.reference
            if self.last is not None:
                self.cleaned[self.last.generation_id] = time.monotonic()
            return result
        self.f.pipeline.publish_candidate = observed
        self.closed = False
        if known_kinds:
            original_scope = self.f.source.open_question_scope
            @contextmanager
            def known(account, budget):
                with original_scope(account, budget) as scope:
                    conversations = scope.conversations
                    def records(question, work):
                        for conversation in conversations(question, work):
                            yield replace(conversation, messages=tuple(
                                replace(message, kind="message", language="en") for message in conversation.messages))
                    scope.conversations = records
                    yield scope
            self.f.source.open_question_scope = known

    def capture_current_reference(self):
        """Record the validated active handle before testing rejection after restart."""
        from app.analytics.generation_reference import GenerationReference
        from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
        from app.analytics.opaque_refs import account_ref
        from app.analytics.query_contracts import utc_instant
        store = getattr(self.f.stores.projections, "_store", None) or self.f.stores.projections
        with store.database.read() as db:
            row = db.execute("SELECT g.*,q.projection_generation,q.first_source FROM projection_generations g "
                "JOIN projection_query_metadata q USING(generation_id,creator_account_id) "
                "WHERE g.creator_account_id=? AND g.status='active'", (account_ref(self.account),)).fetchone()
        if row is None:
            raise ValueError("restart_active_reference_missing")
        reference = GenerationReference(row['generation_id'], row['creator_account_id'],
            row['canonical_revision'], row['projection_generation'], row['canonical_content_digest'],
            row['pipeline_revision'], row['pipeline_config_digest'], row['pipeline_identity_digest'],
            row['projection_digest'], row['graph_digest'], row['publication_epoch'],
            utc_instant(row['first_source']) + timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)
            if row['first_source'] else None)
        store.check_generation_reference(self.account, reference)
        if not self.f.pipeline.projection_is_current(self.account, reference.source_revision):
            raise ValueError("restart_active_reference_not_current")
        self.last = reference
        return {'generation_id': reference.generation_id, 'source_revision': reference.source_revision,
                'checked_current': True}

    def observe_activation(self):
        store = getattr(self.f.stores.projections, "_store", None) or self.f.stores.projections
        store.crash_hook = lambda stage, generation: self.events.append(
            {"stage": stage, "generation": generation, "at": time.monotonic()})

    def counts(self):
        with self.f.repositories.database.read() as db:
            revision = db.execute("SELECT canonical_revision FROM account_heads WHERE creator_account_id=?", (self.account,)).fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM account_messages WHERE creator_account_id=? AND is_deleted=0", (self.account,)).fetchone()[0]
        return {"messages": count, "revision": revision}

    def add(self, chat=1, name="matrix-live-first"):
        from tests.continuous_analytics_fixture import insert_message, advance
        with self.f.repositories.database.transaction() as db:
            insert_message(db, f"chat-{chat}", name, self.clock - timedelta(microseconds=1), 2)
            advance(db)
        return time.monotonic()

    def edit(self):
        from tests.continuous_analytics_fixture import advance
        with self.f.repositories.database.transaction() as db:
            for index in range(100):
                cursor = db.execute("UPDATE account_messages SET text=? WHERE creator_account_id=? AND message_id=?",
                    (f"Synthetic changed support {index}", self.account, f"matrix-input-{index}"))
                if cursor.rowcount != 1:
                    raise ValueError("edit_fixture_missing")
            advance(db)
        return time.monotonic()

    def delete(self):
        from app.persistence.retention import CreatorVaultRetention
        deletion = CreatorVaultRetention(self.f.repositories.database, clock=lambda: self.clock)
        for index in range(100, 200):
            deletion.delete_message(self.account, f"matrix-input-{index}")
        return time.monotonic()

    def verify(self):
        from tools.qualify_continuous_analytics import reference_artifact
        from app.analytics.generation_reference import GenerationReference
        store = getattr(self.f.stores.projections, "_store", None) or self.f.stores.projections
        if self.last is None:
            with store.database.read() as db:
                row = db.execute("SELECT g.generation_id,q.projection_generation FROM projection_generations g JOIN projection_query_metadata q USING(generation_id,creator_account_id) WHERE g.status='active'").fetchone()
            if row is None:
                raise ValueError("no_active_generation")
            values = store._validate_persisted_generation(row[0], materialize_projection=False)
            generation = row["projection_generation"]
        else:
            generation = self.last.projection_generation
        expected = reference_artifact(self.f, generation, compact=True).projection
        with store.database.read() as db:
            row = db.execute("SELECT * FROM projection_generations WHERE status='active'").fetchone()
        actual = store._validate_persisted_generation(row["generation_id"], materialize_projection=False)
        fields = ("projection_digest", "graph_digest", "canonical_content_digest")
        expected_values = {key: getattr(expected, key) for key in fields}
        actual_values = {key: actual[key] if key in actual else row[key] for key in fields}
        if expected_values != actual_values:
            raise ValueError("independent_rebuild_mismatch")
        self.last = GenerationReference.from_projection(expected, row["generation_id"], row["publication_epoch"])
        return {"independent_rebuild_equal": True, "persisted_content_revalidated": True,
                "expected": expected_values, "actual": actual_values}

    def integrity(self):
        store = getattr(self.f.stores.projections, "_store", None) or self.f.stores.projections
        with store.database.read() as db:
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("foreign_key_failure")
            for kind in ("node", "edge"):
                if db.execute(f"SELECT 1 FROM graph_{kind}_content c WHERE NOT EXISTS (SELECT 1 FROM graph_membership_{kind}s r WHERE r.creator_account_id=c.creator_account_id AND r.content_id=c.content_id) LIMIT 1").fetchone():
                    raise ValueError("orphan_graph_content")
            if db.execute("SELECT 1 FROM graph_membership_pages p WHERE NOT EXISTS (SELECT 1 FROM graph_segment_membership_pages r WHERE r.creator_account_id=p.creator_account_id AND r.page_id=p.page_id) LIMIT 1").fetchone():
                raise ValueError("orphan_graph_page")
            return {"no_unpermitted_orphans": True,
                    "retained_generations": [list(r) for r in db.execute("SELECT status,COUNT(*) FROM projection_generations GROUP BY status")]}

    def close(self):
        from tests.continuous_analytics_fixture import cleanup
        if not self.closed:
            cleanup(self.f)
            self.closed = True
