"""Fresh-process question probes with literal source expectations and unchanged budgets."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import time

from tools.analytics_qualification_fixture import question_plan
from tools.analytics_qualification_idle import async_wait_at_least
from tools.analytics_qualification_workloads import policy


def expected_rows(work, case):
    from app.analytics.opaque_refs import conversation_ref, message_ref
    if case == "empty":
        return []
    rows = []
    for chat in range(1, 101):
        if case == "tied_time" and chat == 1:
            continue
        index = work.size // 2 + chat - 1 + ((work.size - work.size // 2 - chat) // 100) * 100
        rows.append({"conversation_ref": conversation_ref(work.account, f"chat-{chat}"),
            "message_ref": message_ref(work.account, f"chat-{chat}", f"matrix-input-{index}"),
            "at": (work.clock - timedelta(hours=48) + timedelta(seconds=index * 48 * 3600 / work.size)).isoformat()})
    return sorted(rows, key=lambda row: (-__import__("datetime").datetime.fromisoformat(row["at"]).timestamp(), row["conversation_ref"]))


def answer_rows(result):
    return [{"conversation_ref": row.conversation_ref,
             "message_ref": row.evidence[0].message_ref if len(row.evidence) == 1 else None,
             "at": row.latest_evidence_at.isoformat()} for row in result.page.rows]


async def questions(work, journal, process, case, state, configured_at):
    from app.analytics import query_service
    from app.analytics.errors import AnalyticsError
    from app.analytics.query_runtime import QuestionResources, PricingNotQualified
    from app.analytics.scheduling import InProcessProjectionScheduler
    from app.models.analytics import AvailabilityStatus
    resources = QuestionResources(work.f.source, work.f.pipeline)
    scheduler = InProcessProjectionScheduler(work.f.pipeline, reconciliation_interval=30)
    plan = question_plan(work.manifest, case)
    expected = expected_rows(work, case)
    budget_type, budgets = query_service.QuestionBudget, []
    def observe_budget(*args, **kwargs):
        budget = budget_type(*args, **kwargs)
        budgets.append(budget)
        return budget
    query_service.QuestionBudget = observe_budget
    last = [None]
    def call(label, index):
        count, started, answer, error = len(budgets), time.monotonic(), None, None
        try:
            answer = resources.execute(policy(work.account), plan)
        except AnalyticsError as failure:
            error = failure.code
        seconds = time.monotonic() - started
        actual = answer_rows(answer) if answer is not None else None
        correct = (answer is not None and actual == expected[:50]
                   and answer.page.undetermined_conversation_count == (1 if case == "tied_time" else 0)
                   and answer.page.has_more == (len(expected) > 50))
        if answer is not None:
            last[0] = answer
        return {"index": index, "phase": label, "process_instance": process,
                "seconds": seconds, "error": error, "correct": correct,
                "records_examined": budgets[-1].records_examined if len(budgets) > count else None,
                "rows": len(answer.page.rows) if answer is not None else None,
                "truncated": answer.page.truncated if answer is not None else None,
                "actual": actual, "has_more": answer.page.has_more if answer is not None else None,
                "undetermined_conversations": answer.page.undetermined_conversation_count if answer is not None else None}
    report = {"initial_messages": work.size, "case": case, "state": state,
        "page_size": 50, "question": plan["question"], "filters": plan["filters"], "plan": plan,
        "process_instance": process, "expected": expected, "calls": [], "complete": False}
    try:
        from contextlib import nullcontext
        progress = getattr(work, "qualification_progress", None)
        with progress.phase("question.cold_readiness") if progress else nullcontext():
            resources.start()
            await scheduler.start(recover=True)
            status = await scheduler.wait(work.account)
        report["cold_readiness_seconds"] = time.monotonic() - configured_at
        report["scheduler_availability"] = status.availability.value
        if status.availability != AvailabilityStatus.AVAILABLE:
            raise ValueError("question_runtime_not_ready")
        trace = getattr(work, "startup_trace", None)
        if trace is not None:
            try:
                trace.mark_ready()
            except Exception:
                trace.failed = True
        journal.save("readiness", {k: v for k, v in report.items() if k not in {"calls", "expected"}})
        if state == "idle":
            report["idle_seconds"] = await async_wait_at_least(work.manifest["questions"]["idle_seconds"])
        elif state == "mutated":
            report["mutation"] = {"before": work.counts()}
            work.edit()
            await scheduler.schedule(work.account, work.counts()["revision"])
            status = await scheduler.wait(work.account)
            if status.availability != AvailabilityStatus.AVAILABLE:
                raise ValueError("mutated_question_not_ready")
            report["mutation"].update(after=work.counts(), ready=True)
            journal.save("mutation", report["mutation"])
        report["first_query"] = await asyncio.to_thread(call, "first_query", -1)
        journal.save("first-query", report["first_query"])
        for index in range(work.manifest["questions"]["warmups"] + work.manifest["questions"]["samples"]):
            label = "warmup" if index < work.manifest["questions"]["warmups"] else "measured"
            item = await asyncio.to_thread(call, label, index)
            report["calls"].append(item)
            journal.save("question", item)
        from tools.analytics_qualification import question_statistics
        report["statistics"] = question_statistics(report["calls"], work.manifest["questions"]["warmups"])
        if case == "generation_bound_pagination" and last[0] is not None:
            cursor = last[0].next_cursor
            if cursor is None:
                raise ValueError("pagination_fixture_has_no_second_page")
            second = await asyncio.to_thread(resources.execute, policy(work.account), dict(plan, cursor=cursor))
            report["second_page"] = answer_rows(second)
            report["pagination_complete"] = report["second_page"] == expected[50:] and second.next_cursor is None
            work.edit()
            await scheduler.schedule(work.account, work.counts()["revision"])
            await scheduler.wait(work.account)
            try:
                await asyncio.to_thread(resources.execute, policy(work.account), dict(plan, cursor=cursor))
            except AnalyticsError as failure:
                report["stale_cursor_error"] = failure.code
                report["stale_cursor_rejected"] = failure.code == "analytics_question_cursor_stale"
            else:
                report["stale_cursor_rejected"] = False
        try:
            await asyncio.to_thread(resources.execute, policy(work.account), dict(plan, question="pricing_discussions.v1"))
        except PricingNotQualified:
            report["pricing_disabled"] = True
        else:
            report["pricing_disabled"] = False
        baseline = getattr(work, "prepared_question_baseline", None)
        if baseline is not None and state != "mutated" and case != "generation_bound_pagination":
            report["verification"] = await asyncio.to_thread(work.verify_prepared_reference, baseline)
        else:
            report["verification"] = await asyncio.to_thread(work.verify)
        report["complete"] = True
    finally:
        query_service.QuestionBudget = budget_type
        resources.close()
        report["scheduler_closed"] = await scheduler.close(timeout=10)
        report["detached_workers"] = scheduler.detached_worker_count
        report["backlog"] = scheduler.retained_account_count
        journal.save("questions", report)
        work.close()
    return report
