"""Bounded cooperative identity work preserves foreground and worker progress."""

from contextlib import ExitStack
from threading import Event, Thread

import pytest

from app.analytics.errors import ProjectionBuildCancelled
from app.analytics.question_work import (
    BACKGROUND_PAUSE_SECONDS, BACKGROUND_RECORDS_PER_SLICE,
    BACKGROUND_WORK_SLICE_SECONDS, QuestionWork,
)

pytestmark = [pytest.mark.ci_tier("fast")]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def consume_records(consume, count=BACKGROUND_RECORDS_PER_SLICE):
    for _ in range(count):
        consume()


def test_idle_preparation_has_no_sleep():
    clock, pauses = Clock(), []
    work = QuestionWork(clock=clock, sleep=pauses.append)
    consume = work.background()
    consume_records(consume, 100_000)
    assert not pauses
    clock.now += BACKGROUND_WORK_SLICE_SECONDS
    consume()
    assert not pauses


def test_two_admitted_requests_keep_pressure_until_both_exit():
    pauses = []
    work = QuestionWork(clock=Clock(), sleep=pauses.append)
    consume = work.background()
    with work.foreground():
        with work.foreground():
            consume_records(consume, BACKGROUND_RECORDS_PER_SLICE - 1)
            assert not pauses
            consume()
            assert pauses == [BACKGROUND_PAUSE_SECONDS]
        consume_records(consume)
        assert pauses == [BACKGROUND_PAUSE_SECONDS] * 2
    consume_records(consume)
    assert pauses == [BACKGROUND_PAUSE_SECONDS] * 2


def test_additional_resource_owners_do_not_change_admission_or_lose_pressure():
    pauses = []
    work = QuestionWork(clock=Clock(), sleep=pauses.append)
    consume = work.background()
    with ExitStack() as scopes:
        for _ in range(2):
            scopes.enter_context(work.foreground())
        with work.foreground():
            scopes.close()
            consume_records(consume)
    consume_records(consume)
    assert pauses == [BACKGROUND_PAUSE_SECONDS]


def test_elapsed_work_yields_before_record_limit_and_excludes_sleep():
    clock, pauses = Clock(), []

    def sleep(delay):
        pauses.append(delay)
        clock.now += delay

    work = QuestionWork(clock=clock, sleep=sleep)
    consume = work.background()
    with work.foreground():
        clock.now = BACKGROUND_WORK_SLICE_SECONDS
        consume()
        consume()
        assert pauses == [BACKGROUND_PAUSE_SECONDS]
        clock.now += BACKGROUND_WORK_SLICE_SECONDS
        consume()
        assert pauses == [BACKGROUND_PAUSE_SECONDS] * 2


def test_each_background_scan_has_its_own_bounded_slice():
    pauses = []
    work = QuestionWork(clock=Clock(), sleep=pauses.append)
    first, second = work.background(), work.background()
    with work.foreground():
        consume_records(first, BACKGROUND_RECORDS_PER_SLICE - 1)
        consume_records(second, BACKGROUND_RECORDS_PER_SLICE - 1)
        assert not pauses
        first()
        second()
    assert pauses == [BACKGROUND_PAUSE_SECONDS] * 2


def test_continuous_foreground_work_cannot_starve_a_100k_scan():
    clock, pauses = Clock(), []

    def sleep(delay):
        pauses.append(delay)
        clock.now += delay

    work = QuestionWork(clock=clock, sleep=sleep)
    consume = work.background()
    with work.foreground():
        for _ in range(100_000):
            clock.now += 0.000012  # Simulated 1.2s of canonical scan work.
            consume()
    assert len(pauses) == 100_000 // BACKGROUND_RECORDS_PER_SLICE
    assert set(pauses) == {BACKGROUND_PAUSE_SECONDS}
    assert clock.now < 3.0  # A deterministic model, not native latency evidence.


@pytest.mark.parametrize("moment", ["before", "after"])
def test_cancellation_surrounds_every_pause(moment):
    cancelled, pauses = [False], []

    def sleep(delay):
        pauses.append(delay)
        cancelled[0] = True

    work = QuestionWork(clock=Clock(), sleep=sleep)
    consume = work.background(cancellation_check=lambda: cancelled[0])
    with work.foreground():
        consume_records(consume, BACKGROUND_RECORDS_PER_SLICE - 1)
        cancelled[0] = moment == "before"
        with pytest.raises(ProjectionBuildCancelled):
            consume()
    assert pauses == ([] if moment == "before" else [BACKGROUND_PAUSE_SECONDS])
    cancelled[0] = False
    consume_records(work.background())
    assert pauses == ([] if moment == "before" else [BACKGROUND_PAUSE_SECONDS])


def test_cancelled_worker_cannot_start_background_work():
    with pytest.raises(ProjectionBuildCancelled):
        QuestionWork().background(cancellation_check=lambda: True)


def test_idle_checkpoints_still_observe_worker_cancellation():
    cancelled = [False]
    work = QuestionWork(clock=Clock(), sleep=lambda _: pytest.fail("idle pause"))
    consume = work.background(cancellation_check=lambda: cancelled[0])
    consume_records(consume, BACKGROUND_RECORDS_PER_SLICE - 1)
    cancelled[0] = True
    with pytest.raises(ProjectionBuildCancelled):
        consume()


def test_foreground_base_exception_releases_pressure():
    pauses = []
    work = QuestionWork(clock=Clock(), sleep=pauses.append)
    with pytest.raises(KeyboardInterrupt):
        with work.foreground():
            raise KeyboardInterrupt()
    consume_records(work.background())
    assert not pauses


def test_sleep_base_exception_remains_the_original_exception():
    failure = KeyboardInterrupt()

    def sleep(delay):
        raise failure

    work = QuestionWork(clock=Clock(), sleep=sleep)
    with pytest.raises(KeyboardInterrupt) as error:
        with work.foreground():
            consume_records(work.background())
    assert error.value is failure
    consume_records(work.background())


def test_background_sleep_never_holds_the_foreground_counter_lock():
    sleeping, release, entered = Event(), Event(), Event()
    failures = []

    def sleep(delay):
        assert delay == BACKGROUND_PAUSE_SECONDS
        sleeping.set()
        assert release.wait(3)

    work = QuestionWork(clock=Clock(), sleep=sleep)

    def background():
        try:
            consume_records(work.background())
        except BaseException as error:
            failures.append(error)

    def foreground():
        try:
            with work.foreground():
                entered.set()
        except BaseException as error:
            failures.append(error)

    with work.foreground():
        scanner = Thread(target=background)
        caller = Thread(target=foreground)
        scanner.start()
        try:
            assert sleeping.wait(3)
            caller.start()
            assert entered.wait(3)
        finally:
            release.set()
            scanner.join(3)
            if caller.ident is not None:
                caller.join(3)
    assert not scanner.is_alive() and not caller.is_alive()
    assert not failures
