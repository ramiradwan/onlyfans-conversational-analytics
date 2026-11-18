"""Out-of-process progress diagnostics; never inspect live Python frames."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time


class Journal:
    """Follow complete JSONL records without re-reading the entire journal."""
    def __init__(self, now):
        self.offset = 0
        self.last = {}
        self.changed = now
        self.warned = False

    def poll(self, path, now):
        try:
            with path.open("rb") as stream:
                stream.seek(self.offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        break  # The writer may still be flushing this record.
                    self.offset = stream.tell()
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    self.last = event
                    self.changed = now
                    self.warned = False
        except OSError:
            # The writer may be creating or closing the journal. Retry next poll.
            pass

    def warning(self, name, now, threshold):
        if (self.warned or self.last.get("event") == "session_finish"
                or now - self.changed < threshold):
            return None
        self.warned = True
        event = self.last.get("event", "no journal records")
        node = self.last.get("nodeid", "(no active test recorded)")
        phase = self.last.get("when", "")
        return (f"[ci-watchdog] {name}: no progress for {now - self.changed:.1f}s; "
                f"last event={event}, phase={phase}, test={node}")


def monitor(directory, threshold, distributed, stop):
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    journals = {}
    # The controller aggregates interleaved xdist events, so it cannot identify
    # an individual stalled worker. Monitor each worker independently.
    expected = "progress-gw*.jsonl" if distributed else "progress.jsonl"
    waiting = Journal(started)
    with (directory / "watchdog.log").open("w", encoding="utf-8") as log:
        def emit(message):
            log.write(message + "\n")
            log.flush()
            try:
                print(message, flush=True)
            except (OSError, UnicodeError):
                pass

        emit(f"[ci-watchdog] external progress monitor started; threshold={threshold:g}s")
        while not stop.is_set():
            now = time.monotonic()
            for path in directory.glob(expected):
                journal = journals.setdefault(path, Journal(now))
                journal.poll(path, now)
            if not journals:
                message = waiting.warning("collection/startup", now, threshold)
                if message:
                    emit(message)
            for path, journal in journals.items():
                message = journal.warning(path.stem, now, threshold)
                if message:
                    emit(message)
            stop.wait(min(5.0, threshold / 4))
        emit("[ci-watchdog] external progress monitor stopped")


def start(directory, seconds, *, distributed):
    threshold = float(seconds)
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("CI_PYTEST_WATCHDOG_SECONDS must be positive and finite")
    # Only the controller owns this pipe. EOF stops the monitor even if pytest
    # exits unexpectedly. No worker stack, memory, or process is inspected.
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(directory.resolve()),
         str(threshold), *(["--distributed"] if distributed else [])],
        stdin=subprocess.PIPE, close_fds=True,
    )


def stop(process):
    try:
        process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    if process.returncode:
        print(f"[ci-watchdog] monitor exited {process.returncode}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("threshold", type=float)
    parser.add_argument("--distributed", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.threshold) or args.threshold <= 0:
        parser.error("threshold must be positive and finite")
    finished = threading.Event()

    def await_parent():
        try:
            sys.stdin.buffer.read()
        finally:
            finished.set()

    threading.Thread(target=await_parent, daemon=True).start()
    monitor(args.directory, args.threshold, args.distributed, finished)


if __name__ == "__main__":
    main()
