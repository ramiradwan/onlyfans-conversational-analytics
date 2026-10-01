"""Low-frequency collector progress and disjoint verification timings.

Progress is advisory, never acceptance evidence or a deadline reset. Final phase
records remain durable. No callback runs inside a scheduled visibility interval.
"""
from __future__ import annotations
from contextlib import contextmanager
import os
from pathlib import Path
import time

from tools import analytics_qualification as q

VERIFICATION_SCHEMA = "analytics-verification-timing.v1"
COMPONENTS = ("resolve_generation", "canonical_reference", "persisted_generation", "compare")


class CollectorProgress:
    def __init__(self, directory: Path, instance: str):
        self.path = directory / "progress.json"
        self.instance = instance
        self.sequence = 0

    def record(self, stage: str, state: str, **details) -> None:
        self.sequence += 1
        value = dict(schema="analytics-collector-progress.v1", process_instance=self.instance,
                     sequence=self.sequence, observed=q.stamp(), stage=stage, state=state, **details)
        temporary = self.path.with_suffix(".pending")
        temporary.write_bytes(q.encoded(value))
        os.replace(temporary, self.path)
        print(f"[collector] {stage} {state}", flush=True)

    @contextmanager
    def phase(self, name: str):
        started = time.monotonic()
        self.record(name, "started")
        try:
            yield
        except BaseException as error:
            self.record(name, "failed", seconds=time.monotonic() - started,
                        error_type=type(error).__name__)
            raise
        else:
            self.record(name, "complete", seconds=time.monotonic() - started)


class VerificationTimer:
    def __init__(self, progress=None, *, clock=time.monotonic):
        self.clock = clock
        self.progress = progress
        self.started = clock()
        self.components = {}
        self.failed = False

    @contextmanager
    def phase(self, name: str):
        if name not in COMPONENTS or name in self.components:
            raise ValueError("verification_timing_component_invalid")
        started = self.clock()
        if self.progress:
            self.progress.record("verification." + name, "started")
        error_type = None
        try:
            yield
        except BaseException as error:
            self.failed = True
            error_type = type(error).__name__
            raise
        finally:
            self.components[name] = self.clock() - started
            if self.progress:
                self.progress.record("verification." + name, "failed" if error_type else "complete",
                                     seconds=self.components[name], error_type=error_type)

    def result(self) -> dict:
        if self.failed or tuple(self.components) != COMPONENTS:
            raise ValueError("verification_timing_incomplete")
        ended = self.clock()
        return dict(schema=VERIFICATION_SCHEMA, started=self.started, ended=ended,
                    total_seconds=ended - self.started, components_seconds=dict(self.components))
