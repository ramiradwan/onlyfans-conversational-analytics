"""Measure a local native probe with frozen identities and Windows lifetime counters."""
from pathlib import Path
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import time


class MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in ("peak_working_set", "working_set", "quota_peak_paged",
        "quota_paged", "quota_peak_nonpaged", "quota_nonpaged", "pagefile", "peak_pagefile")]


def held_handle_peak(child):
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    api = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    api.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    api.restype = wintypes.BOOL
    if not api(int(child._handle), ctypes.byref(counters), ctypes.sizeof(counters)):
        raise OSError("held process memory query failed")
    return counters.peak_working_set


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def validate_provenance(frozen, executable, model, runtime, recipe, inputs):
    if frozen.get("schema") != "native-probe-provenance.v1":
        raise ValueError("invalid provenance schema")
    source = Path(frozen["source_root"])
    build = Path(frozen["build_root"])
    checkpoint = Path(frozen["checkpoint_root"])
    if frozen.get("source_revision") != "1bcf40e80c114ceb707d172b3efd28b577121b39":
        raise ValueError("unexpected source revision")
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != frozen["source_revision"] or subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip():
        raise ValueError("source checkout mismatch")
    required = {executable, runtime, runtime.with_name("onnxruntime_providers_shared.dll"),
                runtime.parent.parent / "LICENSE", runtime.parent.parent / "ThirdPartyNotices.txt",
                recipe, inputs, Path(__file__), Path(__file__).with_name("main.rs"),
                Path(__file__).with_name("prepare.py"), Path(__file__).with_name("Cargo.lock"),
                build / "Cargo.toml", build / "Cargo.lock", build / "src/main.rs",
                build / "upstream/Cargo.toml", Path(frozen["export_manifest"]),
                Path(frozen["approved_reference"]), Path(frozen["approved_freeze"])}
    for name in ("encoder.onnx", "classifier.onnx", "boundary_marginals.onnx", "boundary_scorer.onnx",
                 "config.json", "tokenizer.json", "tokenizer_config.json", "encoder_config/config.json", "LICENSE", "NOTICE", "SOURCE_MODEL_CARD.md"):
        required.add(model / name)
    for name in ("model.safetensors", "config.json", "tokenizer.json", "tokenizer_config.json", "encoder_config/config.json"):
        required.add(checkpoint / name)
    required.update(p for p in (build / "upstream/src").rglob("*.rs"))
    required.update(p for p in (source / "src").rglob("*.rs"))
    required.update(source / name for name in ("Cargo.toml", "Cargo.lock", "LICENSE", "NOTICE", "THIRD-PARTY-NOTICES.md"))
    required.update(Path(os.environ["SystemRoot"]) / "System32" / name
                    for name in ("MSVCP140.dll", "MSVCP140_1.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll"))
    entries = frozen.get("files", [])
    supplied = {Path(entry["path"]).resolve(): entry["sha256"] for entry in entries}
    if len(supplied) != len(entries) or not {p.resolve() for p in required} <= set(supplied):
        raise ValueError("missing or duplicate required provenance paths")
    for path, expected in supplied.items():
        if digest(path) != expected:
            raise ValueError("provenance file mismatch")
    if digest(build / "src/main.rs") != digest(Path(__file__).with_name("main.rs")):
        raise ValueError("runner source mismatch")


def source_view_errors(view, freeze, freeze_sha256, reference_identity):
    if (type(reference_identity) is not dict or set(reference_identity) != {"name", "sha256"}
            or freeze.get("files", {}).get(reference_identity.get("name")) != reference_identity.get("sha256")):
        return ["source_view_origin_mismatch"]
    if (type(view) is not dict or set(view) != {"schema", "origin_reference_sha256", "origin_freeze_sha256", "records"}
            or view["schema"] != "refinement-source-view.v1"
            or view["origin_reference_sha256"] != reference_identity["sha256"]
            or view["origin_freeze_sha256"] != freeze_sha256):
        return ["source_view_origin_mismatch"]
    records = view["records"]
    allowed = {"example_id", "split", "text", "language_assumption", "strata"}
    if (type(records) is not list or any(type(row) is not dict or set(row) != allowed
            or type(row["example_id"]) is not str or row["split"] != "refinement"
            or type(row["text"]) is not str or type(row["language_assumption"]) is not str
            or type(row["strata"]) is not list
            or any(tag != "known_unsupported_language" for tag in row["strata"]) for row in records)):
        return ["invalid_source_view_fields"]
    ids = [row["example_id"] for row in records]
    if len(ids) != len(set(ids)) or set(ids) != set(freeze["split_members"]["refinement"]):
        return ["source_view_membership_mismatch"]
    return []


def fixture_eligibility_errors(inputs, reference, split):
    if (type(inputs) is not list or any(type(x) is not dict or set(x) != {"id", "text", "supported"}
            or type(x["id"]) is not str or type(x["text"]) is not str or type(x["supported"]) is not bool for x in inputs)):
        return ["invalid_expected_inputs"]
    if len({x["id"] for x in inputs}) != len(inputs):
        return ["duplicate_expected_ids"]
    approved = {x["example_id"]:x for x in reference if x["split"] == split}
    if not approved or {x["id"] for x in inputs if not x["id"].startswith("measurement.")} != set(approved):
        return ["reference_membership_mismatch"]
    errors = []
    for row in inputs:
        if row["id"].startswith("measurement."):
            continue
        source = approved[row["id"]]
        eligible = source["language_assumption"] == "en" and "known_unsupported_language" not in source["strata"]
        if row["text"] != source["text"] or type(row["supported"]) is not bool or row["supported"] != eligible:
            errors.append("reference_eligibility_mismatch")
    return errors


def completion_errors(expected_inputs, rows, recipe, code, timed_out):
    import math
    errors = []
    if code != 0 or timed_out:
        errors.append("process_incomplete")
    if (type(expected_inputs) is not list or not expected_inputs or any(type(x) is not dict or set(x) != {"id", "text", "supported"}
            or type(x["id"]) is not str or not x["id"] or type(x["text"]) is not str
            or type(x["supported"]) is not bool for x in expected_inputs)):
        return errors + ["invalid_expected_inputs"]
    expected_ids = [x["id"] for x in expected_inputs]
    if len(set(expected_ids)) != len(expected_ids):
        errors.append("duplicate_expected_ids")
    if type(rows) is not list or not rows or any(type(x) is not dict for x in rows):
        return errors + ["invalid_rows"]
    ready = rows[0]
    shutdown = rows[-1]
    if (ready.get("event") != "ready" or shutdown.get("event") != "shutdown"
            or any("event" in row for row in rows[1:-1])):
        errors.append("invalid_event_order")
    if (ready.get("provider") != "CPUExecutionProvider" or type(ready.get("intra_threads")) is not int or ready["intra_threads"] != 2
            or ready.get("tokenizer_parallel") is not False
            or type(ready.get("affinity_mask")) is not int or ready["affinity_mask"].bit_count() != 2
            or type(ready.get("runtime_info")) is not str or not ready["runtime_info"]):
        errors.append("invalid_runtime_configuration")
    if (ready.get("cpu_arena") is not False or any(type(ready.get(field)) is not int or ready[field] != recipe[key]
            for field, key in (("encoded_token_limit", "max_input_tokens"), ("source_words_limit", "max_source_words"),
                               ("source_codepoints_limit", "max_source_codepoints")))):
        errors.append("candidate_configuration_mismatch")
    actual = rows[1:-1]
    if [row.get("id") for row in actual] != expected_ids:
        errors.append("result_identity_mismatch")
    expected_by_id = {x["id"]:x for x in expected_inputs}
    topic_ids = {x["id"] for x in recipe["topics"]}
    concept_ids = {x["id"] for x in recipe["concepts"]}
    previous_peak = 0
    for row in rows:
        peak = row.get("peak_working_set")
        if type(peak) is not int or peak <= 0 or peak < previous_peak:
            errors.append("invalid_peak_counter")
        else:
            previous_peak = peak
    for row in actual:
        if set(row) != {"id", "topics", "mentions", "outcome", "input_tokens", "elapsed_ms", "peak_working_set"}:
            errors.append("invalid_result_fields")
        source = expected_by_id.get(row.get("id")) if type(row.get("id")) is str else None
        tokens = row.get("input_tokens")
        if (source is None or type(tokens) is not int or tokens < 0
                or type(row.get("elapsed_ms")) is not int or row["elapsed_ms"] < 0
                or type(row.get("topics")) is not list or type(row.get("mentions")) is not list):
            errors.append("invalid_result_types")
            continue
        expected = "assessed"
        if not source["supported"]:
            expected = "unsupported"
        elif (len(source["text"]) > recipe["max_source_codepoints"]
                or len(source["text"].split()) > recipe["max_source_words"]
                or tokens > recipe["max_input_tokens"]):
            expected = "partial"
        if row.get("outcome") != expected or (expected == "assessed" and tokens == 0):
            errors.append("outcome_mismatch")
        if expected != "assessed" and (row["topics"] or row["mentions"]):
            errors.append("withheld_findings")
        seen_topics, seen_mentions = set(), set()
        for finding in row["topics"] + row["mentions"]:
            if type(finding) is not dict:
                errors.append("invalid_finding")
                continue
            score = finding.get("score")
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                errors.append("invalid_score")
        for finding in row["topics"]:
            if type(finding) is not dict:
                continue
            identifier = finding.get("topic_id")
            if (set(finding) != {"topic_id", "score"} or type(identifier) is not str
                    or identifier not in topic_ids or identifier in seen_topics):
                errors.append("invalid_topic")
            else:
                seen_topics.add(identifier)
        for finding in row["mentions"]:
            if type(finding) is not dict:
                continue
            identifier = finding.get("concept_id")
            start, end = finding.get("start"), finding.get("end")
            if (set(finding) != {"concept_id", "start", "end", "score"} or type(identifier) is not str
                    or identifier not in concept_ids or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(source["text"])):
                errors.append("invalid_mention")
                continue
            key = (identifier, start, end)
            if key in seen_mentions:
                errors.append("duplicate_mention")
            seen_mentions.add(key)
    return sorted(set(errors))


def measure(executable, model, runtime, recipe, inputs, provenance, output):
    import psutil
    if os.name != "nt" or output.exists():
        raise ValueError("Windows and a new output directory required")
    output.mkdir(parents=True)
    frozen = json.loads(provenance.read_text(encoding="utf-8"))
    validate_provenance(frozen, executable, model, runtime, recipe, inputs)
    expected_inputs = [json.loads(line) for line in inputs.read_text(encoding="utf-8").splitlines()]
    view = json.loads(Path(frozen["approved_reference"]).read_text(encoding="utf-8"))
    accepted_freeze = json.loads(Path(frozen["approved_freeze"]).read_text(encoding="utf-8"))
    if source_view_errors(view, accepted_freeze, digest(Path(frozen["approved_freeze"])), frozen["reference_identity"]):
        raise ValueError("invalid refinement source view")
    if fixture_eligibility_errors(expected_inputs, view["records"], frozen["reference_split"]):
        raise ValueError("fixture eligibility mismatch")
    import shutil
    snapshots = []
    for entry in frozen["files"]:
        path = Path(entry["path"])
        if path.suffix in (".py", ".rs", ".toml", ".lock", ".md") or path in (executable, recipe, inputs):
            relative = Path("source-snapshot") / (str(len(snapshots)) + "-" + path.name)
            (output / relative).parent.mkdir(exist_ok=True)
            shutil.copyfile(path, output / relative)
            snapshots.append(dict(original=str(path), snapshot=str(relative), sha256=entry["sha256"]))
    identity = dict(snapshots=snapshots, provenance=frozen, provenance_sha256=digest(provenance),
                    native_sha256=digest(executable), runtime_sha256=digest(runtime),
                    recipe_sha256=digest(recipe), input_sha256=digest(inputs),
                    harness_sha256=digest(Path(__file__)), python=sys.version, psutil=psutil.__version__,
                    platform=platform.platform(), available_memory_before=psutil.virtual_memory().available)
    (output / "before-run.json").write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ, ORT_DYLIB_PATH=str(runtime), TOKENIZERS_PARALLELISM="false",
               RAYON_NUM_THREADS="1", OMP_NUM_THREADS="2", MKL_NUM_THREADS="2")
    events = queue.Queue()
    started = time.perf_counter()
    with inputs.open("rb") as source, (output / "stderr.txt").open("wb") as errors:
        child = subprocess.Popen([str(executable), str(model), str(recipe)], stdin=source,
                                 stdout=subprocess.PIPE, stderr=errors, env=env)
        def read():
            for line in child.stdout:
                events.put(line)
        reader = threading.Thread(target=read)
        reader.start()
        samples, rows, known, os_peaks = [], [], {}, {}
        modules = []
        phase = "load"
        deadline = time.monotonic() + 180
        previous = time.perf_counter()
        max_gap = 0
        timed_out = False
        try:
            while child.poll() is None or reader.is_alive() or not events.empty():
                now = time.perf_counter()
                max_gap = max(max_gap, now-previous)
                previous = now
                try:
                    parent = psutil.Process(child.pid)
                    for process in [parent] + parent.children(recursive=True):
                        known[(process.pid, process.create_time())] = process
                except psutil.NoSuchProcess:
                    pass
                rss = private = threads = 0
                affinities = []
                thread_cpu = []
                for key, process in list(known.items()):
                    try:
                        memory = process.memory_info()
                        rss += memory.rss
                        private += getattr(memory, "private", 0)
                        threads += process.num_threads()
                        affinities.append(process.cpu_affinity())
                        thread_cpu += [dict(pid=process.pid, tid=t.id, user=t.user_time, system=t.system_time)
                                       for t in process.threads()]
                        os_peaks[str(key)] = max(os_peaks.get(str(key), 0), memory.peak_wset)
                    except psutil.NoSuchProcess:
                        pass
                samples.append(dict(seconds=now-started, phase=phase, rss=rss, private=private,
                                    threads=threads, affinities=affinities, thread_cpu=thread_cpu))
                while not events.empty():
                    row = json.loads(events.get())
                    rows.append(row)
                    if row.get("event") == "ready":
                        phase = "first_inference"
                        try:
                            module_paths = sorted({mapping.path for mapping in psutil.Process(child.pid).memory_maps()
                                                   if mapping.path.lower().endswith((".dll", ".exe"))})
                            modules = [dict(path=path, bytes=Path(path).stat().st_size, sha256=digest(Path(path)))
                                       for path in module_paths]
                        except psutil.NoSuchProcess:
                            modules = []
                    elif row.get("id") == "measurement.max_words": phase = "max_input_complete"
                    elif row.get("id"): phase = "warm_inference"
                if time.monotonic() > deadline:
                    timed_out = True
                    child.kill()
                    break
                time.sleep(.005)
        finally:
            if child.poll() is None:
                child.kill()
            code = child.wait()
            reader.join()
        final_os_peak = held_handle_peak(child)
    (output / "results.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    (output / "samples.json").write_text(json.dumps(samples, indent=2) + "\n", encoding="utf-8")
    in_worker_peak = max((row.get("peak_working_set", 0) for row in rows), default=0)
    receipt = dict(identity=identity, exit_code=code, timeout=timed_out,
                   elapsed_seconds=time.perf_counter()-started,
                   peak_process_tree_sampled_rss=max(x["rss"] for x in samples),
                   final_os_peak_held_process_handle=final_os_peak,
                   in_worker_phase_os_peak=in_worker_peak,
                   sum_observed_process_os_peaks=sum(os_peaks.values()),
                   process_identities=[dict(pid=k[0],creation_time=k[1]) for k in known],
                   process_tree_coverage="sampled descendants; native source contains no child launch",
                   loaded_modules=modules,
                   peak_private_bytes=max(x["private"] for x in samples),
                   peak_threads=max(x["threads"] for x in samples),
                   load_peak_sampled_rss=max(x["rss"] for x in samples if x["phase"] == "load"),
                   max_sampling_gap_seconds=max_gap, memory_limit_bytes=536870912,
                   native_runtime_rows=[row for row in rows if row.get("event")],
                   settings={k:env[k] for k in ("TOKENIZERS_PARALLELISM","RAYON_NUM_THREADS","OMP_NUM_THREADS","MKL_NUM_THREADS")})
    expected_inputs = [json.loads(line) for line in inputs.read_text(encoding="utf-8").splitlines()]
    recipe_limits = json.loads(recipe.read_text(encoding="utf-8"))
    errors = completion_errors(expected_inputs, rows, recipe_limits, code, timed_out)
    complete = not errors
    receipt["completion_errors"] = errors
    receipt["completion_status"] = "PASS" if complete else "FAIL"
    receipt["language_support_source"] = "caller/fixture annotation; no language detection demonstrated"
    receipt["phase_peak_semantics"] = "in-worker counters are cumulative lifetime peaks through each event"
    peak = max(final_os_peak, in_worker_peak, receipt["peak_process_tree_sampled_rss"], receipt["sum_observed_process_os_peaks"])
    receipt["observed_worker_memory_status"] = ("PASS" if peak <= 536870912 else "FAIL") if complete else "INVALID"
    receipt["qualification_status"] = "UNQUALIFIED"
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in receipt.items() if k not in ("identity",)}, indent=2))
    return code if complete else 1

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("executable", "model", "runtime", "recipe", "inputs", "provenance", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(measure(**vars(args)))
