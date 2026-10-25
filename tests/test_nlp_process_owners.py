"""Exact process lifetime observations, including retained terminated objects."""
import ctypes
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import nlp_dependencies
from app.services.nlp_dependencies import ProcessOwners
from app.nlp.release_catalog import DependencyRejected

pytestmark = pytest.mark.ci_tier('integration')


def kernel(monkeypatch, *, wait=258, opened=123, times=True, error=5):
    def process_times(handle, *values):
        values[0]._obj.dwLowDateTime = 17
        values[0]._obj.dwHighDateTime = 1
        return times

    api = SimpleNamespace(OpenProcess=Mock(return_value=opened),
        GetProcessTimes=Mock(side_effect=process_times),
        WaitForSingleObject=Mock(return_value=wait), CloseHandle=Mock(return_value=True))
    monkeypatch.setattr(nlp_dependencies, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(ctypes, 'WinDLL', Mock(return_value=api), raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: error, raising=False)
    return api


def test_live_owner_keeps_exact_birth_and_pid_reuse_is_not_live(monkeypatch):
    api = kernel(monkeypatch)
    owner = {'pid': 42, 'birth': str((1 << 32) | 17)}
    assert ProcessOwners().identity(42) == owner
    assert not ProcessOwners().absent(owner)
    assert ProcessOwners().absent({'pid': 42, 'birth': 'old'})
    api.OpenProcess.assert_called_with(0x1000 | 0x00100000, False, 42)
    api.GetProcessTimes.assert_called()
    api.WaitForSingleObject.assert_called_with(123, 0)
    assert api.CloseHandle.call_count == 3


@pytest.mark.parametrize('wait', [0xFFFFFFFF, 1, 300])
def test_failed_or_unknown_wait_refuses_and_closes_handle(monkeypatch, wait):
    api = kernel(monkeypatch, wait=wait)
    with pytest.raises(DependencyRejected, match='nlp_owner_observation'):
        ProcessOwners().identity(42)
    api.CloseHandle.assert_called_once_with(123)


def test_failed_process_times_refuses_and_closes_handle(monkeypatch):
    api = kernel(monkeypatch, times=False)
    with pytest.raises(DependencyRejected, match='nlp_owner_observation'):
        ProcessOwners().identity(42)
    api.WaitForSingleObject.assert_not_called()
    api.CloseHandle.assert_called_once_with(123)


@pytest.mark.parametrize('error,absent', [(87, True), (5, False)])
def test_missing_and_inaccessible_processes_remain_distinct(monkeypatch, error, absent):
    api = kernel(monkeypatch, opened=0, error=error)
    if absent:
        assert ProcessOwners().identity(42) is None
    else:
        with pytest.raises(DependencyRejected, match='nlp_owner_observation'):
            ProcessOwners().identity(42)
    api.GetProcessTimes.assert_not_called()
    api.WaitForSingleObject.assert_not_called()
    api.CloseHandle.assert_not_called()


@pytest.mark.windows_compat
@pytest.mark.skipif(os.name != 'nt', reason='Windows retained process-object semantics')
def test_joined_process_with_exit_code_259_is_absent_while_handle_is_retained():
    child = subprocess.Popen([sys._base_executable, '-c', 'import sys; sys.exit(259)'])
    try:
        assert child.wait(timeout=5) == 259
        assert not child._handle.closed
        assert ProcessOwners().identity(child.pid) is None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child._handle.Close()
