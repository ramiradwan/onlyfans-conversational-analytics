"""Fast deterministic protocol tests, not performance qualification."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tools import analytics_qualification as q
from tools import analytics_qualification_worker as worker
from tools.analytics_qualification_fixture import Workload
from tools.analytics_qualification_progress import CollectorProgress, VerificationTimer, COMPONENTS

import pytest

pytestmark = [pytest.mark.ci_tier("fast")]

MANIFEST = q.read_json(Path(__file__).resolve().parents[1] / "docs/analytics/acceptance-manifest.json")


def probe(case, elapsed=1.0):
    values = {key: "sha256:" + "a" * 64 for key in
              ("canonical_content_digest", "projection_digest", "graph_digest")}
    return dict(case=case, phase="one_committed_message", backlog_before=0, backlog_after=0,
                valid_current_result=True, cleanup_complete=True, stale_reference_rejected=True,
                independent_rebuild_equal=True, persisted_content_revalidated=True,
                expected=values, actual=dict(values), clocks=dict(operation_started=1.0,
                durable_canonical_commit=2.0, activation=2.1,
                first_valid_visible_result=2.0+elapsed, required_cleanup_complete=2.0+elapsed,
                backlog_drained=2.0+elapsed, operation_finished=2.1+elapsed))


class Scenario:
    def __init__(self, repeat=1, miss=None, elapsed=12.0, continuation=False, profile="reference-windows-16g"):
        self.calls = []
        self.miss, self.elapsed, self.verify_error = miss, elapsed, None
        self.close_ok, self.detached, self.close_error = True, 0, False
        self.work = SimpleNamespace(size=100000, manifest=deepcopy(MANIFEST), account="account",
            f=SimpleNamespace(source=None, pipeline=None), add=lambda *args: self.calls.append(("mutate", args)),
            close=lambda: self.calls.append("work-close"), capture_current_reference=lambda: {"checked_current": True})
        self.config = dict(repeat=repeat, job=f"visibility/{profile}/{repeat}", output="not-created",
                           continue_after_visibility_failure=continuation)
        self.journal = SimpleNamespace(save=self.save)

    def save(self, label, value):
        self.calls.append((label, deepcopy(value)))

    async def direct(self, work, journal, resources, name):
        self.calls.append(name)
        self.calls.append("cold-or-rebuild-verified")

    async def scheduled(self, work, journal, resources, scheduler, name, mutation, *, case):
        self.calls.append(("scheduled", case))
        mutation()
        self.calls.append("required-cleanup-complete")
        if self.verify_error == case:
            raise ValueError("independent_rebuild_mismatch")
        value = probe(case, self.elapsed if self.miss == case else 1.0)
        self.calls.append("independent-verification-complete")
        self.save("phase", value)
        return value

    def resources(self, *args):
        def close():
            self.calls.append("resources-close")
            if self.close_error:
                raise RuntimeError("close failed")
        return SimpleNamespace(start=lambda: self.calls.append("resources-start"), close=close)

    def scheduler(self, *args, **kwargs):
        async def start(**kwargs): self.calls.append("scheduler-start")
        async def wait(*args): return SimpleNamespace(availability="available")
        async def close(**kwargs):
            self.calls.append("scheduler-joined")
            return self.close_ok
        return SimpleNamespace(start=start, wait=wait, close=close,
                               detached_worker_count=self.detached, retained_account_count=0)

    @contextmanager
    def installed(self):
        modules = {"app.analytics.query_runtime": SimpleNamespace(QuestionResources=self.resources),
                   "app.analytics.scheduling": SimpleNamespace(InProcessProjectionScheduler=self.scheduler),
                   "app.models.analytics": SimpleNamespace(AvailabilityStatus=SimpleNamespace(AVAILABLE="available"))}
        def child(config, directory, mode):
            self.calls.append("restart-child")
            return asyncio.run(worker.visibility(self.work, self.journal, config, "child", restarted=True))
        with patch.dict("sys.modules", modules), patch.object(worker, "direct", self.direct), \
             patch.object(worker, "scheduled", self.scheduled), patch.object(worker, "child", child), \
             patch.object(worker, "mark_state", lambda *args: self.calls.append(("state", args[1]))), \
             patch.object(worker.asyncio, "sleep", AsyncMock()) as sleep:
            yield sleep

    def run(self):
        with self.installed():
            return worker.collect_visibility(self.work, self.journal, self.config, "parent")


class FailFastTests(unittest.TestCase):
    def test_idle_miss_is_verified_and_joined_without_restart(self):
        case = Scenario(miss="idle/dominant")
        result = case.run()
        self.assertFalse(result["complete"])
        self.assertEqual(result["unexecuted_cases"], ["restarted/small"])
        self.assertNotIn("restart-child", case.calls)
        self.assertTrue(result["scheduler_closed"])
        self.assertEqual(result["detached_workers"], 0)
        phase_index = max(i for i,x in enumerate(case.calls) if isinstance(x, tuple) and x[0]=="phase")
        stop_index = next(i for i,x in enumerate(case.calls) if isinstance(x, tuple) and x[0]=="visibility-stop")
        self.assertLess(phase_index, stop_index)
        self.assertLess(stop_index, case.calls.index("scheduler-joined"))
        errors = q.check_visibility(MANIFEST, case.config["job"], result)
        self.assertIn("visibility_over_ten_seconds:idle/dominant", errors)
        self.assertIn("visibility_case_set_mismatch", errors)

    def test_ordinary_failure_does_not_run_rebuild_idle_or_restart(self):
        case = Scenario(miss="ordinary/dominant")
        result = case.run()
        self.assertEqual(result["unexecuted_cases"], MANIFEST["visibility"]["process_cases"][1][1:])
        self.assertNotIn("unchanged_rebuild", case.calls)
        self.assertEqual(len(result["probes"]), 1)
        self.assertNotIn("restart-child", case.calls)

    def test_success_requires_all_twelve_in_fresh_repetitions(self):
        count = 0
        for repeat in range(3):
            case = Scenario(repeat=repeat)
            with case.installed() as sleep:
                result = worker.collect_visibility(case.work, case.journal, case.config, "parent")
                sleep.assert_awaited_once_with(61)
            self.assertTrue(result["complete"])
            self.assertEqual([p["case"] for p in result["probes"]], MANIFEST["visibility"]["process_cases"][repeat])
            self.assertEqual(result["runtime_processes"], ["parent", "child"])
            self.assertEqual(q.check_visibility(MANIFEST, case.config["job"], result), [])
            self.assertEqual(case.calls.count("scheduler-joined"), 2)
            count += len(result["probes"])
        self.assertEqual(count, 12)

    def test_last_restart_failure_remains_failure_with_full_case_set(self):
        case = Scenario(miss="restarted/small")
        result = case.run()
        self.assertEqual(len(result["probes"]), 4)
        self.assertFalse(result["complete"])
        self.assertEqual(result["unexecuted_cases"], [])
        self.assertTrue(result["restart_scheduler_closed"])
        self.assertIn("visibility_over_ten_seconds:restarted/small", q.check_visibility(MANIFEST, case.config["job"], result))

    def test_exactly_ten_seconds_passes_not_rounded_overage(self):
        for elapsed, expected in [(10.0, True), (10.000001, False)]:
            result = Scenario(miss="ordinary/dominant", elapsed=elapsed).run()
            self.assertEqual(result["complete"], expected)

    def test_constrained_profile_keeps_non_numeric_rule(self):
        case = Scenario(miss="ordinary/dominant", profile="constrained-windows-8g")
        result = case.run()
        self.assertTrue(result["complete"])
        self.assertFalse(q.check_visibility(MANIFEST, case.config["job"], result))

    def test_diagnostic_continuation_never_qualifies(self):
        case = Scenario(miss="ordinary/dominant", continuation=True)
        result = case.run()
        self.assertEqual(len(result["probes"]), 4)
        result["continue_after_visibility_failure"] = True
        self.assertEqual(q.check_payload(MANIFEST, {}, case.config["job"], result), ["continue_after_failure_is_diagnostic_only"])

    def test_legacy_manifest_does_not_silently_change_execution(self):
        case = Scenario(miss="ordinary/dominant")
        del case.work.manifest["visibility"]["fail_fast_after_verified_probe"]
        result = case.run()
        self.assertEqual(len(result["probes"]), 4)
        self.assertNotIn("stopped_after_verified_failure", result)
        self.assertTrue(q.check_visibility(case.work.manifest, case.config["job"], result))

    def test_independent_verification_exception_is_failed_not_verified(self):
        case = Scenario(); case.verify_error = "ordinary/dominant"
        result = case.run()
        self.assertFalse(result["complete"])
        self.assertEqual(result["error"], "independent_rebuild_mismatch")
        self.assertNotIn("stopped_after_verified_failure", result)
        self.assertTrue(result["scheduler_closed"])
        self.assertNotIn("restart-child", case.calls)

    def test_failed_scheduler_join_prevents_restart(self):
        case = Scenario(); case.close_ok = False; case.detached = 1
        result = case.run()
        self.assertFalse(result["complete"])
        self.assertFalse(result["scheduler_closed"])
        self.assertNotIn("restart-child", case.calls)

    def test_resource_close_exception_still_joins_scheduler_and_closes_work(self):
        case = Scenario(miss="ordinary/dominant"); case.close_error = True
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            case.run()
        self.assertIn("scheduler-joined", case.calls)
        self.assertIn("work-close", case.calls)

    def test_forged_complete_cannot_clear_stop(self):
        case = Scenario(miss="restarted/small")
        result = case.run(); result["complete"] = True
        for p in result["probes"]: p["clocks"] = probe(p["case"])["clocks"]
        self.assertTrue(q.check_visibility(MANIFEST, case.config["job"], result))

    def test_safety_errors_are_shared_with_final_verifier(self):
        for key in ["cleanup_complete", "valid_current_result", "stale_reference_rejected", "persisted_content_revalidated", "independent_rebuild_equal"]:
            value = probe("ordinary/dominant"); value[key] = False
            self.assertTrue(q.check_visibility_probe(MANIFEST, "reference-windows-16g", value))


class VerificationTests(unittest.TestCase):
    def test_timer_components_are_disjoint_and_complete(self):
        now = [0.0]
        timer = VerificationTimer(clock=lambda: now[0])
        for key in COMPONENTS:
            with timer.phase(key): now[0] += 1.0
        value = timer.result()
        self.assertEqual(value["total_seconds"], 4)
        self.assertEqual(sum(value["components_seconds"].values()), 4)

    def test_failed_or_partial_timer_cannot_report_complete(self):
        timer = VerificationTimer()
        with self.assertRaises(ValueError): timer.result()
        with self.assertRaisesRegex(RuntimeError, "failure"):
            with timer.phase("resolve_generation"): raise RuntimeError("failure")
        with self.assertRaises(ValueError): timer.result()

    def test_workload_keeps_both_recomputations_and_rejects_changed_content(self):
        from tools import qualify_continuous_analytics as reference
        digests = probe("ordinary/small")["expected"]
        counts = {"reference": 0, "actual": 0}
        class Store:
            @contextmanager
            def read(self): yield self
            def execute(self, *args): return self
            def fetchone(self): return {"generation_id": "g", "publication_epoch": "e", "projection_generation": 2}
            def _validate_persisted_generation(self, *args, **kwargs):
                counts["actual"] += 1
                return dict(digests)
        store = Store(); store.database = store
        work = Workload.__new__(Workload)
        work.f = SimpleNamespace(stores=SimpleNamespace(projections=store))
        work.last = SimpleNamespace(projection_generation=2)
        def expected(*args, **kwargs):
            counts["reference"] += 1
            return SimpleNamespace(projection=SimpleNamespace(**digests))
        module = SimpleNamespace(GenerationReference=SimpleNamespace(from_projection=lambda *a: SimpleNamespace(projection_generation=2)))
        with patch.dict("sys.modules", {"app.analytics.generation_reference": module}), patch.object(reference, "reference_artifact", expected):
            for _ in range(2):
                value = work.verify()
                self.assertEqual(value["expected"], value["actual"])
                self.assertEqual(tuple(value["verification_timings"]["components_seconds"]), COMPONENTS)
            self.assertEqual(counts, {"reference": 2, "actual": 2})
            with patch.object(store, "_validate_persisted_generation", return_value=dict(digests, graph_digest="sha256:"+"b"*64)):
                with self.assertRaisesRegex(ValueError, "independent_rebuild_mismatch"): work.verify()

    def test_progress_is_advisory_and_reports_failure(self):
        with TemporaryDirectory() as directory, patch("builtins.print"):
            progress = CollectorProgress(Path(directory), "pid:instance")
            with self.assertRaisesRegex(ValueError, "bad"):
                with progress.phase("restart.storage_initialization"): raise ValueError("bad")
            record = q.read_json(Path(directory)/"progress.json")
            self.assertEqual(record["state"], "failed")
            self.assertEqual(record["process_instance"], "pid:instance")
            self.assertFalse((Path(directory)/"result.json").exists())


class StartupTraceTests(unittest.TestCase):
    def test_nested_time_is_not_double_counted_and_remainder_visible(self):
        from app.core import lifecycle_receipts as lr
        now = [0.0]; trace = lr.StartupTrace("child", clock=lambda: now[0]);trace.start()
        try:
            now[0] = 1
            with lr.startup_span("opening"):
                now[0] = 2
                with lr.startup_span("validation"):
                    lr.startup_count("persisted_recomputations")
                    now[0] = 5
                now[0] = 7
            now[0] = 8
            result = trace.finish()
        finally:
            trace.finish(False)
        self.assertTrue(result["complete"])
        self.assertEqual(result["total_seconds"], 8)
        self.assertEqual(result["covered_seconds"], 6)
        self.assertEqual(result["unexplained_seconds"], 2)
        self.assertEqual([s["exclusive_seconds"] for s in result["spans"]], [3, 3])
        self.assertEqual(result["counters"]["persisted_recomputations"], 1)

    def test_missing_end_failure_and_capacity_never_claim_complete(self):
        from app.core import lifecycle_receipts as lr
        trace = lr.StartupTrace("partial"); trace.start()
        trace.begin("unfinished", {})
        self.assertFalse(trace.finish()["complete"])
        trace = lr.StartupTrace("error");trace.start()
        with self.assertRaises(RuntimeError):
            with lr.startup_span("operation"):raise RuntimeError("operation failed")
        self.assertFalse(trace.finish()["complete"])
        trace = lr.StartupTrace("bounded");trace.start()
        for i in range(trace.MAX_SPANS+1):
            with lr.startup_span("short"):pass
        self.assertEqual(len(trace.spans),trace.MAX_SPANS)
        self.assertFalse(trace.finish()["complete"])

    def test_inactive_or_failed_observer_does_not_change_operation(self):
        from app.core import lifecycle_receipts as lr
        @lr.startup_timed("example", "calls")
        def operation(value):return value+1
        self.assertEqual(operation(2),3)
        trace=lr.StartupTrace("fault");trace.start()
        with patch.object(trace,"begin",side_effect=ValueError("telemetry failure")):
            self.assertEqual(operation(3),4)
        self.assertFalse(trace.finish()["complete"])
        self.assertIsNone(lr._startup_observer)


    def test_inflight_span_crossing_readiness_is_finalized_after_join(self):
        from app.core import lifecycle_receipts as lr
        now=[0.0];trace=lr.StartupTrace('boundary',clock=lambda:now[0]);trace.start()
        now[0]=1
        with lr.startup_span('concurrent_recheck'):
            now[0]=2;trace.mark_ready()
            with lr.startup_span('after_readiness_not_part_of_startup'):
                lr.startup_count('after_readiness_not_counted')
                now[0]=5
        now[0]=6;result=trace.finish()
        self.assertTrue(result['complete'])
        self.assertEqual(result['total_seconds'],2)
        self.assertEqual(result['observed_through'],6)
        self.assertEqual(len(result['spans']),1)
        self.assertEqual(result['spans'][0]['inclusive_seconds'],1)
        self.assertEqual(result['spans'][0]['observed_end'],5)
        self.assertTrue(result['spans'][0]['continued_after_readiness'])
        self.assertNotIn('after_readiness_not_counted',result['counters'])

    def test_unjoined_span_remains_incomplete_after_boundary(self):
        from app.core import lifecycle_receipts as lr
        trace=lr.StartupTrace('unjoined');trace.start();trace.begin('open',{});trace.mark_ready()
        self.assertFalse(trace.finish()['complete'])


    def test_child_exit_and_existing_source_metadata_survive_trace_finalization(self):
        import os
        import tempfile
        from pathlib import Path
        from tools import analytics_qualification as q
        from tools import analytics_qualification_worker as worker
        class Input:
            def __init__(self,*args,**kwargs):pass
            def counts(self):return {'messages':400,'revision':1}
            def close(self):pass
        async def answer(work,*args):
            work.startup_trace.mark_ready()
            return {'complete':True}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'collector'
            manifest=q.read_json(Path(__file__).resolve().parents[1]/'docs/analytics/acceptance-manifest.json')
            config={'mode':'questions-child','output':str(out),'subject_directory':str(root),'subject_sha256':'checked-source',
                    'subject_files':{},'data':str(root/'data'),'manifest':manifest,'messages':400,'case':'populated','state':'fresh',
                    'profile':'constrained-windows-8g','known_kinds':True,'semantic_questions':True,'hardware':{'actual':'hardware'}}
            q.write_once(root/'input.json',config)
            with patch.dict(os.environ,{'OFCA_QUALIFICATION_PROCESS':'owned-child'}),patch.object(worker,'subject_matches',return_value=True) as source_check,patch.object(q,'runtime_context',return_value={'runtime':'test'}),patch.object(worker,'Workload',Input),patch.object(worker,'questions',answer):
                self.assertEqual(worker.main(str(root/'input.json')),0)
            payload=q.read_json(out/'payload.json')
            self.assertTrue(payload['subject_unchanged'])
            self.assertEqual(source_check.call_count,2)
            self.assertEqual(payload['subject_sha256'],config['subject_sha256'])
            self.assertEqual(payload['collector_process']['subject_sha256'],config['subject_sha256'])
            self.assertEqual(payload['manifest_sha256'],q.digest(manifest))
            self.assertEqual(payload['evidence_track'],'semantic_questions')
            self.assertEqual(payload['hardware'],config['hardware'])
            self.assertTrue(payload['startup_timing_complete'])
            self.assertTrue(q.read_json(out/'startup-timings.json')['complete'])


if __name__ == "__main__":
    unittest.main()
