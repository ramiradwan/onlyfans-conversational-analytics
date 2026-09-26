"""Scheduler and mutation collectors behind the versioned baseline command."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import time

from tools.analytics_qualification_fixture import question_plan


def policy(account):
    from app.security.runtime_policy import AuthContext, AuthorizationEpoch, RuntimePolicy
    return RuntimePolicy(AuthContext("synthetic-principal", account, "creator"), AuthorizationEpoch(1))


def observed_question(work, resources):
    from app.analytics.errors import AnalyticsError
    started = time.monotonic()
    try:
        result = resources.execute(policy(work.account), question_plan(work.manifest, "populated"))
        current = result.question.snapshot.source_revision == work.counts()["revision"]
        return {"at": time.monotonic(), "seconds": time.monotonic() - started,
                "error": None, "current": current}
    except AnalyticsError as error:
        return {"at": time.monotonic(), "seconds": time.monotonic() - started,
                "error": error.code, "current": False}


def operation_record(work, name, before, start, committed, visible, drained, **extra):
    generation = work.last.generation_id if work.last else None
    activated = next((event["at"] for event in work.events
                      if event["stage"] == "activated" and event["generation"] == generation), None)
    return {"phase": name, "source_before": before, "source_after": work.counts(),
            "clocks": {"operation_started": start, "durable_canonical_commit": committed,
                "activation": activated, "first_valid_visible_result": visible,
                "required_cleanup_complete": work.cleaned.get(generation),
                "backlog_drained": drained, "operation_finished": time.monotonic()},
            "publication_events": [event for event in work.events if event["at"] >= start], **extra}


async def direct(work, journal, resources, name):
    before, started = work.counts(), time.monotonic()
    work.observe_activation()
    journal.save("operation-started", {"phase": name, "source_before": before, "at": started})
    def build():
        candidate = work.f.pipeline.build_candidate(work.account, force=True)
        work.f.pipeline.publish_candidate(candidate)
    await asyncio.to_thread(build)
    # Use the ordinary readiness check; do not fill an identity cache in the harness.
    await asyncio.to_thread(work.f.pipeline.projection_is_current, work.account, before["revision"])
    result = await asyncio.to_thread(observed_question, work, resources)
    item = operation_record(work, name, before, started, None,
                            result["at"] if result["current"] else None, time.monotonic(),
                            observation=result)
    journal.save("operation", item)
    verified_at = time.monotonic()
    item.update(await asyncio.to_thread(work.verify))
    item["independent_verification_seconds"] = time.monotonic() - verified_at
    journal.save("phase", item)
    return item


async def scheduled(work, journal, resources, scheduler, name, mutation, *, case=None):
    from app.analytics.errors import CanonicalRevisionChanged
    from app.models.analytics import AvailabilityStatus
    before, previous, started = work.counts(), work.last, time.monotonic()
    backlog_before = scheduler.retained_account_count
    work.observe_activation()
    journal.save("operation-started", {"phase": name, "source_before": before, "at": started, "case": case})
    committed = await asyncio.to_thread(mutation)
    journal.save("canonical-commit", {"phase": name, "at": committed, "source_after": work.counts()})
    rejected = False
    if previous is not None:
        try:
            await asyncio.to_thread(work.f.stores.projections.read_generation_artifact, work.account, previous)
        except CanonicalRevisionChanged:
            rejected = True
        else:
            raise ValueError("stale_generation_readable_after_mutation")
    after = work.counts()
    await scheduler.schedule(work.account, after["revision"])
    observations = []
    async def visible_result():
        deadline = time.monotonic() + work.manifest["limits"]["whole_worker_seconds"]
        while time.monotonic() < deadline:
            result = await asyncio.to_thread(observed_question, work, resources)
            observations.append(result)
            if result["current"]:
                return result["at"]
            await asyncio.sleep(work.manifest["measurement"]["visibility_poll_seconds"])
        return None
    observer = asyncio.create_task(visible_result())
    try:
        state = await scheduler.wait(work.account)
        drained = time.monotonic()
        journal.save("backlog-drained", {"phase": name, "at": drained,
            "required_cleanup_complete": work.cleaned.get(work.last.generation_id) if work.last else None,
            "availability": state.availability.value})
        visible = await observer
        if state.availability != AvailabilityStatus.AVAILABLE or state.attempted_revision != after["revision"]:
            raise ValueError("scheduled_revision_not_available")
    finally:
        if not observer.done():
            observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
    item = operation_record(work, name, before, started, committed, visible, drained,
        stale_reference_rejected=rejected, availability_observations=observations,
        backlog_before=backlog_before, backlog_after=scheduler.retained_account_count,
        valid_current_result=visible is not None, cleanup_complete=work.last.generation_id in work.cleaned)
    if case:
        item["case"] = case
    journal.save("operation", item)
    verified_at = time.monotonic()
    item.update(await asyncio.to_thread(work.verify))
    item["independent_verification_seconds"] = time.monotonic() - verified_at
    journal.save("phase", item)
    return item


async def historical(work, journal, resources, scheduler):
    from tests.continuous_analytics_fixture import insert_message, advance
    from app.analytics.errors import CanonicalRevisionChanged
    from app.models.analytics import AvailabilityStatus
    before, previous, started = work.counts(), work.last, time.monotonic()
    work.observe_activation()
    journal.save("operation-started", {"phase": "10000_historical_interleaved_with_100_live", "source_before": before, "at": started})
    commits, stale = [], False
    for batch in range(work.manifest["history"]["batches"]):
        def ingest():
            with work.f.repositories.database.transaction() as db:
                for offset in range(work.manifest["history"]["messages_per_batch"]):
                    number = batch * work.manifest["history"]["messages_per_batch"] + offset
                    insert_message(db, "chat-0", f"matrix-history-{number}",
                        work.clock - timedelta(days=30) + timedelta(seconds=number), number)
                insert_message(db, f"chat-{1 + batch}", f"matrix-interleaved-live-{batch}",
                    work.clock - timedelta(microseconds=100 - batch), batch)
                advance(db)
            return time.monotonic()
        committed = await asyncio.to_thread(ingest)
        revision = work.counts()["revision"]
        commits.append({"revision": revision, "at": committed})
        if batch == 0:
            try:
                await asyncio.to_thread(work.f.stores.projections.read_generation_artifact, work.account, previous)
            except CanonicalRevisionChanged:
                stale = True
            else:
                raise ValueError("historical_stale_reference_readable")
        await scheduler.schedule(work.account, revision)
        await asyncio.sleep(work.manifest["history"]["producer_pause_seconds"])
    producer_end = time.monotonic()
    state = await scheduler.wait(work.account)
    drained = time.monotonic()
    if state.availability != AvailabilityStatus.AVAILABLE or state.attempted_revision != commits[-1]["revision"]:
        raise ValueError("historical_backlog_not_drained")
    observation = await asyncio.to_thread(observed_question, work, resources)
    item = operation_record(work, "10000_historical_interleaved_with_100_live", before, started,
        commits[0]["at"], observation["at"] if observation["current"] else None, drained,
        stale_reference_rejected=stale, commits=commits, producer_finished=producer_end,
        backlog_after=scheduler.retained_account_count, observation=observation,
        cleanup_included=True, historical_messages=work.manifest["history"]["batches"] * work.manifest["history"]["messages_per_batch"], live_messages=work.manifest["history"]["batches"])
    journal.save("operation", item)
    verified_at = time.monotonic()
    item.update(await asyncio.to_thread(work.verify))
    item["independent_verification_seconds"] = time.monotonic() - verified_at
    journal.save("phase", item)
    return item


async def matrix(work, journal):
    from app.analytics.query_runtime import QuestionResources
    from app.analytics.scheduling import InProcessProjectionScheduler
    resources = QuestionResources(work.f.source, work.f.pipeline, clock=lambda: work.clock)
    scheduler = InProcessProjectionScheduler(work.f.pipeline, worker_count=1, queue_capacity=64)
    report = {"phases": [], "complete": False}
    try:
        report["phases"].append(await direct(work, journal, resources, "cold"))
        report["phases"].append(await direct(work, journal, resources, "unchanged_rebuild"))
        await scheduler.start(recover=False)
        for name, mutation in (("one_committed_message", work.add), ("100_edits", work.edit),
                               ("100_creator_deletions", work.delete)):
            report["phases"].append(await scheduled(work, journal, resources, scheduler, name, mutation))
        report["phases"].append(await historical(work, journal, resources, scheduler))
        report.update(await asyncio.to_thread(work.integrity))
        report["backlog"] = scheduler.retained_account_count
        report["complete"] = True
    finally:
        resources.close()
        report["scheduler_closed"] = await scheduler.close(timeout=10)
        report["detached_workers"] = scheduler.detached_worker_count
        journal.save("matrix", report)
        work.close()
    return report
