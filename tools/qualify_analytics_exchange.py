"""Run named synthetic exchange cases without opening application data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import platform


def run_case(case, directory, *, process=None, publication_checks=False):
    from app.analytics.exchange_codec import decode_exchange
    from app.analytics.query_execution import QuestionLimits
    from tools.analytics_exchange_cosmos import CosmosExchangeTarget
    from tools.analytics_exchange_fixtures import SyntheticSource
    from tools.analytics_exchange_local import SQLiteExchangeTarget, execute_question

    source = SQLiteExchangeTarget(directory / "source", SyntheticSource(case))
    restored = SQLiteExchangeTarget(directory / "restored", SyntheticSource(case))
    remote = None
    limits = QuestionLimits(wall_clock_ms=30000)
    try:
        raw = source.export(source.build())
        expected = execute_question(source, source.source, limits=limits).page
        source.source.assert_expected(expected)
        if process is not None:
            remote = CosmosExchangeTarget(process, source.source)
            remote.import_bytes(raw)
            remote.import_bytes(raw)
            actual = execute_question(remote, source.source, limits=limits).page
            source.source.assert_expected(actual)
            if actual != expected:
                raise ValueError("exchange_question_mismatch")
            raw = remote.export()
        receipt = restored.import_bytes(raw)
        if restored.export(receipt) != raw or execute_question(restored, restored.source, limits=limits).page != expected:
            raise ValueError("exchange_roundtrip_mismatch")
        checks = []
        if process is not None and publication_checks:
            checks = check_remote_publication(process, source.source, raw)
        return {"case": case["id"], "status": "passed", "content_digest": decode_exchange(raw).content_digest,
                "publication_checks": checks, "metrics": {} if remote is None else dict(remote.metrics)}
    finally:
        try:
            if remote is not None:
                remote.cleanup()
        finally:
            source.close()
            restored.close()


def check_remote_publication(process, source, raw):
    from app.analytics.exchange_codec import ExchangeInvalid, decode_exchange
    from tools.analytics_exchange_cosmos import CosmosExchangeTarget

    results = []
    for checkpoint in ("vertices", "records", "validated", "published"):
        target = CosmosExchangeTarget(process, source)
        class Interrupted(Exception):
            pass
        def interrupt(stage):
            if stage == checkpoint:
                raise Interrupted()
        try:
            try:
                target.import_bytes(raw, fault=interrupt)
            except Interrupted:
                pass
            else:
                raise ValueError("exchange_interruption_missing")
            if (target.request("read_manifest") is not None) != (checkpoint == "published"):
                raise ValueError("exchange_partial_publication")
            target.import_bytes(raw)
            target.import_bytes(raw)
            if target.export() != raw:
                raise ValueError("exchange_resume_mismatch")
            results.append({"checkpoint": checkpoint, "status": "passed", "metrics": dict(target.metrics)})
        finally:
            target.cleanup()
    target = CosmosExchangeTarget(process, source)
    original_now = source.now
    due = decode_exchange(raw).snapshot.retention_due_at
    try:
        if due is not None:
            def expire(stage):
                if stage == "validated":
                    source.now = due
            try:
                target.import_bytes(raw, fault=expire)
            except ExchangeInvalid:
                pass
            else:
                raise ValueError("exchange_expiry_missing")
            if target.request("read_manifest") is not None:
                raise ValueError("exchange_expired_publication")
            results.append({"checkpoint": "expiry", "status": "passed", "metrics": dict(target.metrics)})
    finally:
        source.now = original_now
        target.cleanup()
    return results


def source_evidence():
    root = Path(__file__).resolve().parents[1]
    def git(*args):
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    from app.analytics.exchange_codec import question_definitions
    relative = ["tools/analytics_cosmos_node/package-lock.json", "tools/analytics_cosmos_node/templates.cjs",
        "tools/analytics_cosmos_node/adapter.cjs", "tools/analytics_cosmos_node/runner.cjs",
        "app/analytics/exchange_contracts.py", "app/analytics/exchange_codec.py",
        "tools/analytics_exchange_records.py", "tools/analytics_exchange_local.py",
        "tools/analytics_exchange_cosmos.py", "tools/analytics_cosmos_process.py",
        "tools/analytics_exchange_fixtures.py", "tools/qualify_analytics_exchange.py",
        "tests/fixtures/analytics/questions/no-later-reply.json",
        "tests/fixtures/analytics/questions/pricing-discussions.json"]
    patch = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=root, capture_output=True, check=True).stdout
    return {"source_sha": git("rev-parse", "HEAD"), "clean_tree": not bool(git("status", "--porcelain")),
            "tracked_diff_digest": hashlib.sha256(patch).hexdigest(),
            "python_version": platform.python_version(), "question_definitions": question_definitions(),
            "input_digests": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in relative}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("local", "cosmos"), default="local")
    parser.add_argument("--fixture", choices=("no-later-reply", "pricing-discussions", "all"), default="all")
    args = parser.parse_args(argv)
    required = ("ANALYTICS_GREMLIN_ENDPOINT", "ANALYTICS_GREMLIN_KEY",
                "ANALYTICS_GREMLIN_DATABASE", "ANALYTICS_GREMLIN_GRAPH")
    if args.target == "cosmos" and not all(os.environ.get(key) for key in required):
        print(json.dumps({"schema_version": "analytics-exchange-conformance.v1", "status": "not_configured"}))
        return 0
    os.environ["ENVIRONMENT"] = "test"
    os.environ["WEBSOCKET_AUTH_MODE"] = "development_stub"
    os.environ["CANONICAL_PERSISTENCE_BACKEND"] = "memory"
    os.environ["OFCA_TEST_DATABASE_MASTER_KEY_HEX"] = secrets.token_hex(32)
    from tools.analytics_cosmos_process import NodeGremlinProcess
    from tools.analytics_exchange_fixtures import FIXTURE_NAMES, load_cases
    process = None
    current_case = None
    try:
        if args.target == "cosmos":
            process = NodeGremlinProcess()
        names = FIXTURE_NAMES if args.fixture == "all" else (args.fixture,)
        cases = [case for name in names for case in load_cases(name)]
        results = []
        with tempfile.TemporaryDirectory(prefix="analytics-exchange-") as directory:
            for name, filename in (("AUTH_DATABASE_PATH", "auth.sqlite3"),
                    ("CANONICAL_DATABASE_PATH", "canonical.sqlite3"),
                    ("PROJECTION_DATABASE_PATH", "projections.sqlite3"),
                    ("ANALYTICS_PROJECTION_DATABASE_PATH", "analytics-projections.sqlite3")):
                os.environ[name] = str(Path(directory) / filename)
            for index, case in enumerate(cases):
                current_case = case["id"]
                results.append(run_case(case, Path(directory) / str(index), process=process,
                                        publication_checks=index == 0))
        if process is not None:
            process.close()
            process = None
        print(json.dumps({"schema_version": "analytics-exchange-conformance.v1", "status": "passed",
            "target": args.target, "cases": results, **source_evidence()}, sort_keys=True))
        return 0
    except Exception as error:
        from app.analytics.query_execution import QuestionLimitExceeded
        code = "analytics_question_limit_exceeded" if isinstance(error, QuestionLimitExceeded) else "analytics_exchange_conformance_failed"
        print(json.dumps({"schema_version": "analytics-exchange-conformance.v1", "status": "failed",
                          "target": args.target, "case": current_case, "code": code}))
        return 1
    finally:
        if process is not None:
            try:
                process.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
