"""Exercise immutable packages through admitted capture and authenticated UI."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import threading
import time
import tempfile

from tools import analytics_qualification as q
from tools.analytics_qualification_execution import mark_state
from tools.analytics_qualification_fixture import question_plan
from tools.analytics_qualification_package_process import PackagedProcess, receipt_account
from tools.analytics_qualification_package_fixture import Fixture
from tools.analytics_qualification_tracks import check_unknown_question


class Browser:
    def __init__(self, root, inputs, output):
        self.output, self.index, self.responses = output, 0, queue.Queue()
        self.process = subprocess.Popen([shutil.which("node"), str(Path(root) /
            "tools/e2e-capture/lib/packaged-analytics-browser.mjs")], cwd=root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            runtime, received = self.call("start", inputs=inputs)
            q.write_once(self.output / "browser-start.json", {"received_ns": received, "runtime": runtime})
        except BaseException:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
            self.reader.join(timeout=1)
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line) > 1048576:
                    raise ValueError("browser_receipt_budget_exceeded")
                self.responses.put(json.loads(line))
        except (OSError, ValueError) as error:
            self.responses.put(error)
        finally:
            self.responses.put(EOFError())

    def call(self, action, **values):
        self.index += 1
        self.process.stdin.write(json.dumps(dict(values, action=action, id=self.index)) + "\n")
        self.process.stdin.flush()
        try:
            response = self.responses.get(timeout=120)
        except queue.Empty:
            raise ValueError("browser_command_deadline_exceeded") from None
        received = time.monotonic_ns()
        if isinstance(response, BaseException):
            raise ValueError("browser_receipt_unavailable") from None
        if response.get("id") != self.index or response.get("ok") is not True:
            raise ValueError("browser_command_failed:" + action)
        value = response["value"]
        if action not in {"start", "question", "ui"}:
            q.write_once(self.output / f"browser-{self.index:06}-{action}.json",
                         {"action": action, "received_ns": received, "result": value})
        return value, received

    def close(self):
        if self.process.poll() is None:
            self.call("close")
            self.process.stdin.close()
            self.process.wait(timeout=10)
        self.reader.join(timeout=1)
        if self.reader.is_alive() or self.process.returncode != 0:
            raise ValueError("browser_shutdown_incomplete")


class Resources:
    """Sample the enclosing owned Windows job and isolated working directories."""
    def __init__(self, directories):
        self.directories = tuple(Path(p) for p in directories)
        self.stop_event, self.error = threading.Event(), None
        self.peak_memory, self.peak_disk, self.samples = 0, 0, 0
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        from tools.analytics_qualification_process import WindowsJob
        job = WindowsJob()
        job.close()
        try:
            while not self.stop_event.is_set():
                info = job.info_type()
                if not job.kernel.QueryInformationJobObject(None, 9, ctypes.byref(info), ctypes.sizeof(info), None):
                    raise OSError("owned_job_memory_unavailable")
                self.peak_memory = max(self.peak_memory, int(info.PeakJob))
                total = 0
                for directory in self.directories:
                    for path in directory.rglob("*"):
                        try:
                            if path.is_file():
                                total += path.stat().st_size
                        except FileNotFoundError:
                            continue
                self.peak_disk = max(self.peak_disk, total)
                self.samples += 1
                self.stop_event.wait(1)
        except OSError:
            self.error = "package_resources_unavailable"

    def start(self):
        self.thread.start()

    def finish(self):
        self.stop_event.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive() or self.error or not self.samples:
            raise ValueError(self.error or "package_resource_sampling_incomplete")
        return {"process_tree_peak_bytes": self.peak_memory,
                "temporary_disk_peak_bytes": self.peak_disk, "samples": self.samples,
                "disk_sampling_seconds": 1, "memory_scope": "owned_job_kernel_peak",
                "disk_scope": "data_browser_profile_and_owned_temporary_directories"}


class Workload:
    def __init__(self, config):
        self.config, self.inputs, self.manifest = config, config["inputs"], config["manifest"]
        self.directory = Path(config["output"])
        self.directory.mkdir(parents=True, exist_ok=False)
        for name in tuple(os.environ):
            if name.startswith("OFCA_TEST_"):
                del os.environ[name]
        temporary = self.directory / "temporary"
        temporary.mkdir()
        os.environ.update(TEMP=str(temporary), TMP=str(temporary))
        tempfile.tempdir = str(temporary)
        self.instance = f"{os.getpid()}:{secrets.token_hex(32)}"
        self.processes, self.current, self.browser = [], None, None
        self.phase_index, self.last, self.all_clean = 0, None, True
        self.observation_index, self.refusals = 0, set()
        self.plan = question_plan(self.manifest, "populated")
        self.browser_inputs = dict(self.inputs, evaluation_clock=self.manifest["fixture"]["evaluation_clock"])
        q.write_once(self.directory / "process.json", {"pid": os.getpid(), "instance": self.instance,
            "supervisor_instance": os.environ["OFCA_QUALIFICATION_PROCESS"]})
        q.write_once(self.directory / "hardware.json", config["observed"]["hardware"])

    def start(self):
        process = PackagedProcess(Path(self.inputs["runtime_directory"]) / "Brain.exe",
            self.inputs["data_directory"], self.directory / f"runtime-{len(self.processes):02}",
            secrets.token_hex(32)).start()
        self.processes.append(process)
        self.current = process
        deadline = time.monotonic() + 120
        while not process.receipts.snapshot():
            if process.process.poll() is not None or time.monotonic() > deadline:
                raise ValueError("packaged_startup_unavailable")
            time.sleep(.05)
        startup = process.receipts.snapshot()[0]
        clock = time.get_clock_info("monotonic")
        if (startup["clock_implementation"] != clock.implementation
                or abs(startup["clock_resolution_ns"] - clock.resolution * 1_000_000_000) > .01):
            raise ValueError("parent_child_clock_domain_mismatch")
        self.browser = Browser(self.config["root"], dict(self.browser_inputs,
            seeded_size=self.last["source_after"]["messages"] if self.last else 0),
            self.current.output / "browser")
        return time.monotonic_ns()

    def stop(self):
        errors = []
        for operation in (lambda: self.browser.close() if self.browser else None,
                          lambda: self.current.stop(self.manifest["limits"]["shutdown_seconds"]) if self.current else None):
            try:
                operation()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                errors.append(type(error).__name__)
        self.browser, self.current = None, None
        if errors:
            raise ValueError("packaged_product_cleanup_incomplete")

    def events(self):
        account = receipt_account(self.current.run_id, self.inputs["synthetic_account_id"])
        return [event for event in self.current.receipts.snapshot()
                if event.get("account_ref") == account]

    def source(self):
        commits = [event for event in self.events() if event["event_type"] == "canonical_commit"]
        if commits:
            return {"messages": commits[-1]["after_message_count"], "revision": commits[-1]["canonical_revision"]}
        if self.last:
            return self.last["source_after"].copy()
        raise ValueError("canonical_source_not_observed")

    def current_result(self, messages, *, after_ns=0, generation=None):
        while True:
            value, received = self.browser.call("question", plan=self.plan)
            if value.get("status") not in {200, 503, 409}:
                raise ValueError("packaged_question_request_failed")
            body = value.get("body", {})
            if value.get("status") != 200:
                detail = body.get("detail", {}) if isinstance(body, dict) else {}
                reason = detail.get("code") if isinstance(detail, dict) else None
                refusal = (value.get("status"), reason)
                if refusal not in self.refusals and len(self.refusals) < 16:
                    self.refusals.add(refusal)
                    q.write_once(self.directory / "observations" / f"refusal-{len(self.refusals):02}.json",
                        {"status": refusal[0], "code": refusal[1], "received_ns": received})
            if value.get("status") == 200 and check_unknown_question(body):
                snapshot = body["question"]["snapshot"]
                events = self.events()
                latest = [e for e in events if e["event_type"] == "canonical_commit"]
                revision = latest[-1]["canonical_revision"] if latest else snapshot["source_revision"]
                related = [e for e in events if e.get("generation_id") == snapshot["generation_id"]
                           and e.get("canonical_revision") == revision and e["monotonic_ns"] >= after_ns]
                lifecycle = {event["event_type"]: event for event in related}
                drained = lifecycle.get("scheduler_drained", {})
                if (snapshot["source_message_count"] == messages and snapshot["source_revision"] == revision
                        and (generation is None or snapshot["generation_id"] != generation)
                        and {"activation_commit", "cleanup_complete", "scheduler_drained"} <= lifecycle.keys()
                        and all(drained.get(key) == 0 for key in
                                ("pending_accounts", "recovery_requests", "active_publications"))):
                    visible, visible_ns = self.browser.call("ui")
                    shown = visible.get("body", {})
                    if (visible.get("status") == 200 and visible.get("visible") is True
                            and visible.get("uncertainty_visible") is True
                            and visible.get("uncertainty_guidance_present") is True
                            and visible.get("source_links") == visible.get("positive_rows") == 0
                            and check_unknown_question(shown)
                            and shown["question"]["snapshot"] == snapshot):
                        observation = {"result": shown, "received_ns": visible_ns, "ui": visible}
                        self.observation_index += 1
                        q.write_once(self.directory / "observations" / f"current-{self.observation_index:03}.json", observation)
                        return observation, lifecycle
            time.sleep(self.manifest["measurement"]["visibility_poll_seconds"])

    def seed(self, size):
        self.fixture = Fixture(size, self.manifest["fixture"]["evaluation_clock"])
        for offset in range(0, size, 1000):
            self.browser.call("seed", offset=offset, count=min(1000, size - offset), size=size)
        result, lifecycle = self.current_result(size)
        commits = [event for event in self.events() if event["event_type"] == "canonical_commit"]
        if not commits or commits[0]["before_message_count"] != 0:
            raise ValueError("dedicated_empty_synthetic_account_required")
        self.last = {"source_after": self.source(), "generation_id": result["result"]["question"]["snapshot"]["generation_id"]}
        return result, lifecycle

    def append(self, message_id, chat):
        self.browser.call("append", message_id=message_id, chat=chat)
        self.fixture.append(message_id, chat)

    def edit(self):
        self.browser.call("edit")
        self.fixture.edit()

    def delete(self):
        self.browser.call("delete")
        self.fixture.delete()

    def phase(self, name, operation, *, messages, case=None, start_ns=None):
        from tools.analytics_qualification_package_oracle import verify_package
        self.phase_index += 1
        previous = self.last
        before = self.source()
        start_ns = start_ns or time.monotonic_ns()
        sequence = len(self.current.receipts.snapshot())
        request_started_ns = time.monotonic_ns()
        operation()
        observation, lifecycle = self.current_result(messages, after_ns=request_started_ns,
            generation=previous["generation_id"] if previous else None)
        after = self.source()
        commits = [dict(event, receipt_id=f"{self.current.run_id}:{event['sequence']}",
                        operation_count=event["admitted_events"])
                   for event in self.events() if event["event_type"] == "canonical_commit"
                   and event["sequence"] > sequence]
        generation = observation["result"]["question"]["snapshot"]["generation_id"]
        clocks = {"operation_started": start_ns / 1_000_000_000,
            "activation": lifecycle["activation_commit"]["monotonic_ns"] / 1_000_000_000,
            "required_cleanup_complete": lifecycle["cleanup_complete"]["monotonic_ns"] / 1_000_000_000,
            "backlog_drained": lifecycle["scheduler_drained"]["monotonic_ns"] / 1_000_000_000,
            "first_valid_visible_result": observation["received_ns"] / 1_000_000_000,
            "operation_finished": time.monotonic()}
        if commits:
            clocks["durable_canonical_commit"] = commits[0]["monotonic_ns"] / 1_000_000_000
        path = f"verification-{self.phase_index:03}.json"
        verified = verify_package(self.inputs, generation, after["revision"], self.directory / path,
            previous_generation_id=previous["generation_id"] if previous else None,
            expected_messages=self.fixture.rows())
        value = dict(verified, phase=name, source_before=before, source_after=after,
            generation_id=generation, admitted_commits=commits, clocks=clocks, observation=observation,
            process_instance=f"{self.current.process.pid}:{self.current.run_id}",
            lifecycle_sequences={key: lifecycle[key]["sequence"] for key in
                ("activation_commit", "cleanup_complete", "scheduler_drained")}, verification_record=path)
        value["verified_at_ns"] = time.monotonic_ns()
        if case:
            value.update(case=case, backlog_before=0, backlog_after=0,
                         valid_current_result=True, cleanup_complete=True)
        q.write_once(self.directory / f"{self.phase_index:03}-phase.json", value)
        self.last = value
        return value

    def rebuild(self):
        result, _ = self.browser.call("rebuild")
        if result != {"status": 202, "body": {"availability": "building"}}:
            raise ValueError("authenticated_full_rebuild_not_admitted")

    def matrix(self):
        size = self.config["messages"]
        self.seed(size)
        self.stop()
        started = time.monotonic_ns()
        self.start()
        phases = [self.phase("cold", self.rebuild, messages=size, start_ns=started),
                  self.phase("unchanged_rebuild", self.rebuild, messages=size)]
        phases.append(self.phase("one_committed_message", lambda: self.append("matrix-current", 1), messages=size + 1))
        phases.append(self.phase("100_edits", self.edit, messages=size + 1))
        phases.append(self.phase("100_creator_deletions", self.delete, messages=size - 99))
        def history():
            for batch in range(self.manifest["history"]["batches"]):
                self.browser.call("history_batch", batch=batch)
                self.fixture.history_batch(batch)
                time.sleep(self.manifest["history"]["producer_pause_seconds"])
        phases.append(self.phase("10000_historical_interleaved_with_100_live", history,
            messages=size - 99 + 10100))
        return {"phases": phases, "backlog": 0,
                "no_unpermitted_orphans": all(p.get("no_unpermitted_orphans") is True for p in phases)}

    def visibility(self):
        from tools.analytics_qualification_package_oracle import verify_package
        size = self.manifest["visibility"]["messages"]
        mark_state(self.config, "cold", self.instance)
        self.seed(size)
        verify_package(self.inputs, self.last["generation_id"], self.last["source_after"]["revision"],
                       self.directory / "cold-verification.json", expected_messages=self.fixture.rows())
        cases = self.manifest["visibility"]["process_cases"][self.config["repeat"]]
        probes, process_ids = [], []
        for index, case in enumerate(cases):
            state, shape = case.split("/")
            if state == "restarted":
                self.stop()
            mark_state(self.config, state, self.instance)
            if state == "rebuilt":
                self.phase("unchanged_rebuild", self.rebuild, messages=size + index)
            elif state == "idle":
                time.sleep(self.manifest["visibility"]["idle_seconds"])
            elif state == "restarted":
                self.start()
                current, _ = self.browser.call("question", plan=self.plan)
                if (current.get("status") != 200 or not check_unknown_question(current.get("body"))
                        or current["body"]["question"]["snapshot"]["source_revision"] != self.last["source_after"]["revision"]):
                    raise ValueError("restart_current_reference_not_observed")
                q.write_once(self.directory / "restart-reference.json", {"result": current["body"],
                    "process_instance": f"{self.current.process.pid}:{self.current.run_id}"})
            probe = self.phase("one_committed_message", lambda: self.append(
                f"visibility-{index}", 0 if shape == "dominant" else 1),
                messages=size + index + 1, case=case)
            probes.append(probe)
            if probe["process_instance"] not in process_ids:
                process_ids.append(probe["process_instance"])
            errors = q.check_visibility_probe(self.manifest, self.config["profile"], probe)
            if errors:
                return {"probes": probes, "initial_messages": size, "complete": False,
                    "stopped_after_verified_failure": {"case": case, "reasons": errors},
                    "unexecuted_cases": cases[index + 1:]}
        return {"probes": probes, "initial_messages": size, "runtime_processes": process_ids,
                "backlog": 0, "restart_backlog": 0, "restart_detached_workers": 0,
                "restart_scheduler_closed": True, "cold_verification_record": "cold-verification.json"}

    def package(self, startup_ns):
        from tools.analytics_qualification_package_oracle import verify_package
        resources = Resources((self.inputs["data_directory"], self.inputs["browser_profile"], self.directory))
        resources.start()
        try:
            observation, _ = self.seed(self.manifest["fixture"]["sizes"][-1])
            q.write_once(self.directory / "production-question.json", observation["result"])
            q.write_once(self.directory / "production-ui.json", observation)
            admitted = any(e["event_type"] == "canonical_commit" and e.get("origin") == "authorized_agent"
                           and e.get("admitted_events", 0) > 0 for e in self.events())
            paused, _ = self.browser.call("capture_control", control="pause")
            if paused.get("status") != 202 or paused.get("body", {}).get("delivered", 0) < 1:
                raise ValueError("capture_pause_not_admitted")
            while self.browser.call("capture_status")[0].get("mode") != "paused":
                time.sleep(.1)
            q.write_once(self.directory / "capture-paused.json", {"state": self.browser.call("capture_status")[0]})
            resumed, _ = self.browser.call("capture_control", control="resume")
            if resumed.get("status") != 202 or resumed.get("body", {}).get("delivered", 0) < 1:
                raise ValueError("capture_resume_not_admitted")
            while self.browser.call("capture_status")[0].get("mode") != "full":
                time.sleep(.1)
            recovery_phase = self.phase("one_committed_message", lambda: self.append(
                "package-resumed-current", 1), messages=self.manifest["fixture"]["sizes"][-1] + 1)
            request_ns = time.monotonic_ns()
            self.rebuild()
            while True:
                builds = [event for event in self.events() if event["event_type"] == "build_started"
                          and event.get("full_rebuild") is True and event["monotonic_ns"] >= request_ns]
                if builds:
                    break
                time.sleep(.01)
            build = builds[-1]
            cancelling = self.current
            cancelling.stop(self.manifest["limits"]["shutdown_seconds"])
            self.browser.close()
            self.current, self.browser = None, None
            cancelled = [event for event in cancelling.receipts.snapshot()
                         if event["event_type"] == "build_cancelled"
                         and event.get("attempt_id") == build.get("attempt_id")]
            cancellation = {"process_instance": f"{cancelling.process.pid}:{cancelling.run_id}",
                "request_ns": request_ns, "attempt_id": build["attempt_id"],
                "started_sequence": build["sequence"],
                "cancelled_sequence": cancelled[-1]["sequence"] if cancelled else None}
            q.write_once(self.directory / "cancellation.json", cancellation)
            self.start()
            recovered, _ = self.browser.call("ui")
            recovery = recovered.get("status") == 200 and check_unknown_question(recovered.get("body"))
            if not recovery:
                raise ValueError("packaged_recovery_question_unavailable")
            snapshot = recovered["body"]["question"]["snapshot"]
            verify_package(self.inputs, snapshot["generation_id"], snapshot["source_revision"],
                self.directory / "recovery-verification.json", expected_messages=self.fixture.rows())
            q.write_once(self.directory / "recovery-ui.json", recovered)
        finally:
            measurements = resources.finish()
        q.write_once(self.directory / "resources.json", measurements)
        artifacts = self.config["observed"]["artifacts"]
        return dict(measurements, installer_bytes=Path(artifacts["installer"]["path"]).stat().st_size,
            unpacked_bytes=sum(p.stat().st_size for p in Path(self.inputs["runtime_directory"]).rglob("*") if p.is_file()),
            startup_seconds=(startup_ns - self.processes[0].started_ns) / 1_000_000_000,
            ui=observation["ui"]["visible"], authorized_ingestion=admitted,
            cancellation=bool(cancelled),
            recovery=recovery, offline=self.config["observed"]["network_isolation"] == {"active_adapters": 0, "default_routes": 0},
            phases=[recovery_phase], production_unknown_questions=check_unknown_question(observation["result"]),
            dependencies=sorted(self.config["observed"]["runtime_files"]), memory_scope="process_tree",
            artifact_hashes_verified=True)


def collect(config):
    from tools.analytics_qualification_packaged import artifact_context, extracted_files
    work = Workload(config)
    artifacts = {key: value["sha256"] for key, value in config["observed"]["artifacts"].items()}
    payload = {"complete": False, "execution": "packaged", "evidence_track": "packaged",
        "ingestion_path": "authorized_agent", "fixture_mode": "production_unknown_kinds",
        "profile": config["profile"], "hardware": config["observed"]["hardware"],
        "artifact_hashes": artifacts, "subject_sha256": config["subject_sha256"],
        "supervisor_instance": os.environ["OFCA_QUALIFICATION_PROCESS"]}
    try:
        q.write_once(work.directory / "network-isolation-before.json", config["observed"]["network_isolation"])
        startup = work.start()
        result = (work.package(startup) if config["mode"] == "package" else
                  work.matrix() if config["mode"] == "matrix" else work.visibility())
        payload.update(result)
        work.stop()
        payload.update(scheduler_closed=True, detached_workers=0)
        after = {key: value["sha256"] for key, value in artifact_context(work.inputs).items()}
        files = extracted_files(config["observed"]["artifacts"]["runtime"]["path"], work.inputs["runtime_directory"])
        if files != config["observed"]["runtime_files"]:
            raise ValueError("extracted_package_changed")
        from tools.analytics_qualification_hardware import observe_network_isolation
        q.write_once(work.directory / "network-isolation-after.json", observe_network_isolation())
        q.write_once(work.directory / "artifact-check.json", {"before": artifacts, "after": after})
        payload["complete"] = result.get("complete", True)
    except (OSError, ValueError, KeyError, ImportError, subprocess.SubprocessError) as error:
        payload.update(complete=False, failure=type(error).__name__)
        try:
            work.stop()
        except (OSError, ValueError, subprocess.SubprocessError):
            payload["cleanup_failed"] = True
    finally:
        payload["ended_ns"] = time.monotonic_ns()
        q.write_once(work.directory / "payload.json", payload)
    return payload
