"""Wait for the measured idle minimum, including early timer wake-ups."""
from __future__ import annotations

import asyncio
import time


async def async_wait_at_least(seconds: float) -> float:
    started, elapsed = time.monotonic(), 0.0
    resolution = time.get_clock_info("monotonic").resolution
    while elapsed < seconds:
        await asyncio.sleep(max(seconds - elapsed, resolution))
        elapsed = time.monotonic() - started
    return elapsed


def wait_at_least(seconds: float) -> float:
    started, elapsed = time.monotonic(), 0.0
    resolution = time.get_clock_info("monotonic").resolution
    while elapsed < seconds:
        time.sleep(max(seconds - elapsed, resolution))
        elapsed = time.monotonic() - started
    return elapsed


async def timer_preflight(seconds: float) -> dict:
    """Exercise both real wait paths concurrently, without starting product work."""
    from tools.analytics_qualification import finite

    if not finite(seconds) or seconds == 0:
        raise ValueError("invalid_idle_minimum")
    asynchronous, synchronous = await asyncio.gather(
        async_wait_at_least(seconds), asyncio.to_thread(wait_at_least, seconds))
    elapsed = {"asynchronous": asynchronous, "synchronous": synchronous}
    errors = [name + "_idle_minimum_not_met" for name, value in elapsed.items()
              if not finite(value) or value < seconds]
    clock = time.get_clock_info("monotonic")
    return dict(status="FAIL" if errors else "PASS", errors=errors,
                required_seconds=seconds, elapsed_seconds=elapsed,
                clock=dict(implementation=clock.implementation, resolution=clock.resolution),
                event_loop=type(asyncio.get_running_loop()).__name__, qualification_credit=0)


def main() -> int:
    import argparse
    import os
    from pathlib import Path
    import sys
    from tools import analytics_qualification as q

    parser = argparse.ArgumentParser(description="Check native idle timers before expensive qualification jobs.")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if Path(__file__).resolve() != (args.source_root / "tools/analytics_qualification_idle.py").resolve():
        parser.error("Run the preflight module from the selected qualification source.")
    if os.name != "nt" or sys.version_info[:2] != (3, 11):
        parser.error("Run with the qualification's pinned Windows Python 3.11 interpreter.")
    if args.output.exists():
        parser.error("Use a new preflight receipt path.")
    source = q.source_context(args.source_root)
    if source["working_tree"] or not source["signature_valid"]:
        parser.error("A clean signed qualification source is required.")
    manifest = q.read_json(args.source_root / "docs/analytics/acceptance-manifest.json")
    seconds = max(manifest[track]["idle_seconds"] for track in ("questions", "visibility"))
    report = asyncio.run(timer_preflight(seconds))
    if q.source_context(args.source_root) != source:
        report["errors"].append("source_changed_during_preflight")
        report["status"] = "FAIL"
    report.update(source_sha256=q.digest(source), manifest_sha256=q.digest(manifest), runtime=q.runtime_context())
    q.write_once(args.output, report)
    print(report["status"] + ": native idle timer preflight; qualification credit 0")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
