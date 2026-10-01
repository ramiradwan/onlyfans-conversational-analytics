"""Focused A07 insertion diagnostics, called by the existing light runner.

Component results measure content algorithms, never scheduler visibility or trust
admission. The scheduled scope checks the real path at a smaller account size.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
import inspect
from pathlib import Path
import threading
import time

SCHEMA = "a07-focused-insertion.v1"
MAX_EVENTS = 4096


class Attribution:
    """Bounded bulk-call spans. No row callbacks, SQL text, or source values."""
    def __init__(self, *, enabled=True, max_events=MAX_EVENTS):
        self.enabled = enabled
        self.max_events = max_events
        self.phase = "preparation"
        self.events = []
        self.patches = []
        self.local = threading.local()
        self.lock = threading.Lock()
        self.sequence = 0

    @contextmanager
    def span(self, name, **counts):
        if not self.enabled:
            yield {}
            return
        with self.lock:
            self.sequence += 1
            index = self.sequence
            if index > self.max_events:
                raise RuntimeError("focused_trace_capacity_exceeded")
        stack = getattr(self.local, "stack", [])
        self.local.stack = stack
        item = dict(id=index, parent=stack[-1]["id"] if stack else None,
                    name=name, phase=self.phase, thread=threading.get_ident(),
                    counts=counts, child_seconds=0.0, child_cpu_seconds=0.0)
        stack.append(item)
        start, cpu = time.monotonic(), time.thread_time()
        try:
            yield item
        except BaseException as error:
            item["error"] = type(error).__name__
            raise
        finally:
            elapsed, used = time.monotonic()-start, time.thread_time()-cpu
            stack.pop()
            item.update(start=start, end=start+elapsed, seconds=elapsed,
                        thread_cpu_seconds=used,
                        self_seconds=max(0.0, elapsed-item.pop("child_seconds")),
                        self_cpu_seconds=max(0.0, used-item.pop("child_cpu_seconds")))
            if stack:
                stack[-1]["child_seconds"] += elapsed
                stack[-1]["child_cpu_seconds"] += used
            with self.lock:
                self.events.append(item)

    def patch(self, owner, name, before=None, after=None):
        original = getattr(owner, name)
        if inspect.isgeneratorfunction(original) or inspect.iscoroutinefunction(original):
            raise ValueError("focused_trace_requires_synchronous_bulk_call")
        label = owner.__name__ + "." + name
        @wraps(original)
        def measured(*args, **kwargs):
            if not self.enabled:
                return original(*args, **kwargs)
            counts = before(args, kwargs) if before else {}
            with self.span(label, **counts) as item:
                answer = original(*args, **kwargs)
                if after:
                    item["counts"].update(after(answer))
                if answer is None or type(answer) is bool:
                    item["outcome"] = "none" if answer is None else str(answer).lower()
                else:
                    item["outcome"] = "returned"
                return answer
        self.patches.append((owner, name, original))
        setattr(owner, name, measured)

    def install(self, *, pipeline=False):
        from app.analytics import conversation_insertion as insertion
        from app.analytics import conversation_append as append
        from app.analytics import conversation_enrichment_insertion as inserted
        from app.analytics import conversation_enrichment_units as units
        from app.analytics import conversation_enrichment_unit_sql as storage
        from app.analytics import metrics, source_snapshot
        self.patch(insertion, "match_inserted_source",
            lambda a,k: dict(source_records=len(a[1]["messages"]), previous_records=len(a[3])),
            lambda r: {} if r is None else dict(insertion_index=r[0],
                reconstructed_metric_inputs=0 if r[3] is None else len(r[3]), shifted_records=len(r[2])-r[0]-1))
        self.patch(metrics, "build_conversation_metrics_from_bound_values",
            lambda a,k: dict(metric_inputs=len(a[4])) if hasattr(a[4], "__len__") else {})
        self.patch(metrics, "append_conversation_metrics")
        if hasattr(insertion, "build_inserted_metrics"):
            self.patch(insertion, "build_inserted_metrics")
            from app.analytics import tied_insertion_metrics
            self.patch(tied_insertion_metrics, "tied_suffix_metrics",
                lambda a,k: dict(suffix_records=len(a[1])))
        self.patch(insertion, "build_conversation_metrics_from_values")
        self.patch(inserted, "pack_insertion", lambda a,k: dict(previous_records=a[0].header.message_count))
        self.patch(inserted, "validate_inserted_unit", lambda a,k: dict(
            previous_records=0 if a[2] is None else a[2].message_count,
            candidate_records=a[3].header.message_count))
        self.patch(storage, "_validate_appended_unit", lambda a,k: dict(
            previous_records=0 if a[2] is None else a[2].message_count,
            candidate_records=a[3].header.message_count))
        self.patch(storage, "_validate_unit", lambda a,k: dict(full_records=a[0].header.message_count))
        self.patch(storage, "load_unit", after=lambda r: {} if r is None else dict(
            loaded_records=r.header.message_count, compressed_bytes=r.retained_bytes))
        self.patch(units, "message_frame", after=lambda r: dict(frame_bytes=len(r)))
        self.patch(source_snapshot, "conversation_digest", lambda a,k: dict(source_records=len(a[0]["messages"])))
        # Imports bound at module import time need their own wrappers, not global hooks.
        self.patch(insertion, "conversation_digest", lambda a,k: dict(source_records=len(a[0]["messages"])))
        self.patch(insertion, "message_records", after=lambda r: dict(split_records=len(r)))
        self.patch(inserted, "message_records", after=lambda r: dict(split_records=len(r)))
        self.patch(append, "conversation_digest", lambda a,k: dict(source_records=len(a[0]["messages"])))
        if pipeline:
            from app.analytics.canonical_source import HistoryAnalyticsSource
            from app.analytics.pipeline import AnalyticsPipeline
            from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore as Store
            from app.analytics import conversation_graph_insertion, conversation_pages
            for name in ("prepare_question_identity", "_prepare_question_identity", "analytics_snapshot"):
                self.patch(HistoryAnalyticsSource, name)
            self.patch(HistoryAnalyticsSource, "conversation_read_model", after=lambda r:
                {} if r is None else dict(source_records=len(r["messages"])))
            self.patch(source_snapshot, "scan_identity", after=lambda r: dict(scanned_records=r[2]))
            for name in ("build_candidate", "publish_candidate", "prepare_questions"):
                self.patch(AnalyticsPipeline, name)
            for name in ("update_reuse_prepared", "predecessor_update_reuse_prepared",
                         "prepare_update_reuse", "_trusted_verification_envelope",
                         "_trusted_conversation_enrichment_proof", "_trusted_conversation_graph_proof",
                         "stage_built_artifact", "_validate_persisted_generation"):
                if hasattr(Store, name):
                    self.patch(Store, name)
            self.patch(insertion, "try_insert", lambda a,k: proof_counts(a[5]))
            self.patch(append, "try_append", lambda a,k: proof_counts(a[6]))
            self.patch(conversation_graph_insertion, "replace_suffix")
            for name in ("insert_page_sets",):
                if hasattr(conversation_pages, name):
                    self.patch(conversation_pages, name)

    def restore(self):
        for owner, name, original in reversed(self.patches):
            setattr(owner, name, original)
        self.patches.clear()


def proof_counts(loader):
    # Only inspect existing attributes. No database call or proof refresh.
    return {name + "_present": getattr(loader, name, None) is not None for name in
            ("graph_segment_proof", "graph_unit_proof", "enrichment_unit_proof")}


def raw_fixture(total, clock):
    """The qualification's dominant source selection plus its ordinary update."""
    count = total // 2
    rows = [dict(message_id=f"matrix-input-{i}", source_ordinal=i, text="Thanks pricing",
                 sent_at=(clock-timedelta(hours=48)+timedelta(seconds=i*48*3600/total)).isoformat(),
                 direction="inbound" if i % 2 == 0 else "outbound", sentiment=None)
            for i in range(count)]
    rows.append(dict(message_id="visibility-ordinary-dominant", source_ordinal=count,
                     text="Thanks pricing", sent_at=(clock-timedelta(microseconds=1)).isoformat(),
                     direction="inbound", sentiment=None))
    return dict(conversation_id="chat-0", platform_user_id="synthetic-fan", display_name=None,
                unread_count=0, last_message_at=rows[-1]["sent_at"], messages=rows)


def mutate_raw(raw, operation):
    name = "visibility-idle-dominant" if operation == "insert" else "visibility-z-append-dominant"
    rows = [dict(row) for row in raw["messages"]]
    rows.append(dict(rows[-1], message_id=name))
    # All fixture ties use the same stream epoch and source sequence.
    rows.sort(key=lambda row: (row["sent_at"], row["message_id"]))
    rows = [dict(row, source_ordinal=i) for i, row in enumerate(rows)]
    return dict(raw, messages=rows, last_message_at=rows[-1]["sent_at"])


def full_unit(raw, account, cutoff):
    """Fresh model/enrichment recomputation, never a cached expected result."""
    from app.analytics.enrichment import EnrichmentStage
    from app.analytics.metrics import build_conversation_metrics
    from app.analytics.source_snapshot import conversation_digest
    from app.analytics.conversation_enrichment_units import create_enrichment_unit
    from app.models.analytics import CanonicalConversation
    stage = EnrichmentStage()
    conversation = CanonicalConversation.model_validate(raw)
    findings = stage.enrich_conversation(account, conversation)
    metrics = build_conversation_metrics(account, conversation, findings)
    unit = create_enrichment_unit(account_ref=metrics.account_ref, conversation_ref=metrics.conversation_ref,
        input_digest=conversation_digest(raw), config_digest=stage.config_digest, cutoff=cutoff,
        findings=findings, metrics=metrics, analyzer_entries=())
    if unit is None:
        raise ValueError("focused_fixture_unit_rejected")
    return unit


def component_database(previous):
    """Real unit SQL/schema; minimal parent tables, not a publication fixture."""
    from app.persistence import sqlite_api
    from app.analytics.conversation_enrichment_unit_sql import insert_units
    db = sqlite_api.connect(":memory:")
    try:
        sqlite_api.require_cipher(db)
        db.row_factory = sqlite_api.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""CREATE TABLE projection_generations (
            generation_id TEXT, creator_account_id TEXT, status TEXT,
            PRIMARY KEY(generation_id,creator_account_id));
            CREATE TABLE generation_content_epoch (singleton INTEGER PRIMARY KEY,value INTEGER);
            INSERT INTO generation_content_epoch VALUES (1,0);""")
        schema = Path(__file__).resolve().parents[1]/"app/analytics/sql/0017_conversation_enrichment_units.sql"
        db.executescript(schema.read_text(encoding="utf-8"))
        db.execute("INSERT INTO projection_generations VALUES ('previous',?,'building')", (previous.header.account_ref,))
        if insert_units(db, "previous", [previous]) != 1:
            raise ValueError("focused_previous_not_stored")
        db.commit()
        return db
    except BaseException:
        db.close()
        raise


def construct(previous, raw, operation, account, check):
    from app.analytics import conversation_insertion as insertion
    from app.analytics import conversation_enrichment_insertion as inserted
    from app.analytics import conversation_enrichment_units as units
    from app.analytics import metrics
    from app.analytics.enrichment import EnrichmentStage
    from app.analytics.source_snapshot import conversation_digest
    from app.models.analytics import CanonicalConversation
    h = previous.header
    position = len(raw["messages"])-2 if operation == "insert" else len(raw["messages"])-1
    conversation = CanonicalConversation.model_validate(dict(raw, messages=[raw["messages"][position]]))
    added = EnrichmentStage().enrich_conversation(account, conversation)[0]
    digest = conversation_digest(raw)
    if operation == "insert":
        matched = insertion.match_inserted_source(account, raw, h, units.message_records(previous), check)
        if matched is None:
            raise ValueError("focused_insertion_match_rejected")
        index, _, rows, inputs = matched
        rows[index] = units._canonical(added.model_dump(mode="json"))
        if hasattr(insertion, "build_inserted_metrics"):
            # Follow the constructor's production helper; baseline revisions use
            # their original full metric calculation. Instrumentation is identical.
            counts = insertion.build_inserted_metrics(h, rows, index, added, check)
        else:
            inputs.insert(index, metrics.ConversationMetricInput.from_enrichment(added))
            counts = insertion.build_conversation_metrics_from_values(account, conversation, inputs)
        findings = units.InsertedMessageEnrichments(rows, added, index, h.first_source_at)
        result = inserted.pack_insertion(previous, findings, counts, digest, h.config_digest,
                                         h.retention_cutoff, (), check)
    else:
        prefix = dict(raw, messages=raw["messages"][:-1], last_message_at=raw["messages"][-2]["sent_at"])
        if conversation_digest(prefix) != h.input_digest:
            raise ValueError("focused_append_prefix_rejected")
        frame = units.message_frame(previous)
        from app.models.analytics import MessageEnrichment
        last = MessageEnrichment.model_validate_json(frame.rpartition(b"\n")[2])
        counts = metrics.append_conversation_metrics(h.metrics, last, added, raw["unread_count"],
            sentiment_total_factory=lambda: units.sentiment_score_sum(frame, h.message_count, check=check))
        if counts is None:
            raise ValueError("focused_append_metrics_fallback")
        findings = units.AppendedMessageEnrichments(frame, added, None, h.first_source_at, previous=previous)
        result = units.append_enrichment_unit(previous, input_digest=digest, config_digest=h.config_digest,
            cutoff=h.retention_cutoff, findings=findings, metrics=counts, analyzer_entries=(), check=check)
    if result is None:
        raise ValueError("focused_construction_rejected")
    return result


def component_sample(db, previous, raw, operation, account, trace, *, index):
    from app.analytics import conversation_enrichment_unit_sql as sql
    from app.analytics import conversation_enrichment_insertion as inserted
    from app.analytics.conversation_enrichment_units import message_frame, analyzer_frame
    trace.phase = f"sample-{index}/{operation}"
    check = lambda: None
    constructed_at = time.monotonic()
    with trace.span("component.construct", previous_records=previous.header.message_count):
        candidate = construct(previous, raw, operation, account, check)
    construct_seconds = time.monotonic()-constructed_at
    db.execute("SAVEPOINT isolated_sample")
    try:
        stored_at = time.monotonic()
        with trace.span("component.store"):
            db.execute("INSERT INTO projection_generations VALUES ('candidate',?,'building')", (previous.header.account_ref,))
            if sql.insert_units(db, "candidate", [candidate]) != 1:
                raise ValueError("focused_candidate_not_stored")
            persisted = sql.load_unit(db, "candidate", candidate.header.account_ref, candidate.header.conversation_ref)
        store_seconds = time.monotonic()-stored_at
        validated_at = time.monotonic()
        with trace.span("component.validate"):
            appended = sql._validate_appended_unit(db, "previous", previous.header, persisted, check=check)
            accepted = appended or inserted.validate_inserted_unit(db, "previous", previous.header, persisted, check=check)
            if not accepted or appended != (operation == "append"):
                raise ValueError("focused_validation_path_differs")
        validation_seconds = time.monotonic()-validated_at
        # Independent source recomputation stays outside component timing, but is
        # executed for EVERY sample and compared with actual persisted bytes.
        enabled = trace.enabled
        trace.enabled = False
        oracle_started = time.monotonic()
        try:
            expected = full_unit(raw, account, previous.header.retention_cutoff)
            sql._validate_unit(persisted, check=check)
            if (persisted.header != expected.header or message_frame(persisted) != message_frame(expected)
                    or analyzer_frame(persisted) != analyzer_frame(expected)):
                raise ValueError("focused_independent_rebuild_mismatch")
        finally:
            trace.enabled = enabled
        return dict(index=index, operation=operation, accepted_path="append" if appended else "insert",
            construct_seconds=construct_seconds, store_seconds=store_seconds, validation_seconds=validation_seconds,
            independent_rebuild_equal=True, persisted_content_revalidated=True,
            oracle_seconds=time.monotonic()-oracle_started,
            input_digest=candidate.header.input_digest, output_digest=candidate.header.canonical_digest,
            unit_id=candidate.header.unit_id)
    finally:
        db.execute("ROLLBACK TO isolated_sample")
        db.execute("RELEASE isolated_sample")


async def run_component(args, q, light, outer, status, manifest, result, workdir):
    from app.analytics.conversation_enrichment_unit_sql import _validate_unit
    from app.analytics.source_snapshot import conversation_digest
    from tests.continuous_analytics_fixture import ACCOUNT
    clock = datetime.fromisoformat(manifest["fixture"]["evaluation_clock"])
    raw = raw_fixture(args.messages, clock)
    prep = time.monotonic()
    light.atomic_status(status, "focused-component-preparation")
    previous = full_unit(raw, ACCOUNT, clock-timedelta(days=90))
    _validate_unit(previous)
    db = component_database(previous)
    result.update(fixture=dict(account_messages_basis=args.messages,
        previous_conversation_records=previous.header.message_count,
        raw_sha256=conversation_digest(raw), previous_unit_id=previous.header.unit_id,
        previous_unit_independently_validated=True,
        analyzer_entries=0, unchanged_analyzer_cache_payload_excluded=True,
        storage="SQLCipher in-memory, real enrichment-unit migration and minimal parent tables"),
        preparation_seconds=time.monotonic()-prep, samples=[])
    trace = Attribution(enabled=args.trace_mode != "none")
    start = time.monotonic()
    try:
        if trace.enabled:
            trace.install()
        for repeat in range(args.focused_repeats):
            operations = ("append", "insert") if repeat % 2 == 0 else ("insert", "append")
            for operation in operations:
                index = len(result["samples"])
                light.atomic_status(status, "focused-component-sample", index=index, operation=operation)
                before = time.monotonic()
                sample = component_sample(db, previous, mutate_raw(raw, operation), operation, ACCOUNT, trace, index=index)
                sample["sample_with_oracle_seconds"] = time.monotonic()-before
                result["samples"].append(sample)
                q.write_once(args.output/"samples"/f"{index:02}.json", sample)
        result["complete"] = True
    finally:
        trace.restore()
        result["attribution"] = trace.events
        result["iteration_seconds"] = time.monotonic()-start
        db.close()
        result["component_database_closed"] = True
        result["safety"] = dict(scheduler_used=False, publication_authority_tested=False,
                                normal_storage_validator_used=True)
    result["summary"] = dict(samples=len(result["samples"]), previous_records=previous.header.message_count,
                              preparation_seconds=result["preparation_seconds"], iteration_seconds=result["iteration_seconds"])


async def run_update(args, q, light, outer, status, manifest, result, workdir):
    from tools.analytics_qualification_fixture import Workload, Journal
    from tools.analytics_qualification_workloads import direct, scheduled
    from tools.light_first_update_benchmark import summarize_probe
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.scheduling import InProcessProjectionScheduler
    work = Workload(workdir, manifest, args.messages, reopen=False, known_kinds=False)
    resources = QuestionResources(work.f.source, work.f.pipeline)
    scheduler = InProcessProjectionScheduler(work.f.pipeline, worker_count=1,
                                            queue_capacity=64, reconciliation_interval=30)
    journal = Journal(args.output/"events", str(__import__("os").getpid()))
    trace_type = Attribution
    if getattr(args, 'full_attribution', False):
        from tools.analytics_update_attribution import UpdateAttribution
        trace_type = UpdateAttribution
    trace = trace_type(enabled=args.trace_mode != "none")
    original_save = journal.save
    def save(label, value):
        saved = original_save(label, value)
        if label == "operation" and value.get("case"):
            trace.enabled = False
            outer.phase = "independent-verification"
            if getattr(args, 'full_attribution', False):
                q.write_once(args.output/'operation-attribution.json', dict(
                    probe=value, attribution=trace.snapshot(), coarse_events=list(outer.events)))
        return saved
    journal.save = save
    try:
        # Same starting source for append and insertion; no simulated 61-second
        # idle, forced rebuild, recovered process-local proofs, or cold-prefix claim.
        work.add(0, "visibility-ordinary-dominant")
        result["source_before"] = work.counts()
        prep = time.monotonic()
        light.atomic_status(status, "focused-update-cold-preparation")
        await direct(work, journal, resources, "cold")
        resources.start()
        await scheduler.start(recover=True)
        result["preparation_seconds"] = time.monotonic()-prep
        case = "focused/"+args.focused_operation
        trace.phase = outer.phase = case
        if args.trace_mode != "none":
            trace.install(pipeline=True)
            outer.install(q)
        light.atomic_status(status, "focused-update", operation=args.focused_operation)
        name = "visibility-idle-dominant" if args.focused_operation == "insert" else "visibility-z-append-dominant"
        probe = await scheduled(work, journal, resources, scheduler, "one_committed_message",
            lambda: work.add(0, name), case=case)
        result.update(probe=probe, summary=summarize_probe(probe), complete=True)
    finally:
        # The coarse layer is installed last and may wrap the full tracer's
        # connection methods. Unwind in reverse order, including on errors.
        try:
            outer.restore()
            outer.patches.clear()
        finally:
            trace.restore()
        result["attribution"] = trace.events
        if getattr(args, 'full_attribution', False):
            result['full_attribution'] = trace.snapshot()
        try:
            resources.close()
        finally:
            try:
                closed = await scheduler.close(timeout=10)
                result["safety"] = dict(scheduler_closed=closed,
                    detached_workers=scheduler.detached_worker_count, backlog=scheduler.retained_account_count)
                if not closed or scheduler.detached_worker_count or scheduler.retained_account_count:
                    result["complete"] = False
            finally:
                work.close()
