"""Sampling of top-level desktop windows a test can attribute to its own run.

Default-tier tests drive a real Windows installer and start the launcher it
placed.  Either can take desktop focus away from whoever typed ``pytest``, so
the packaging falsifiers observe window creation rather than assume it away.

Attribution cannot rely on the owning process alone.  A console window belongs
to the terminal host rather than to the program running inside it, and the host
is a service child that shares no ancestry with the test; the launched path in
the window title is the only attribution such a window carries.  Predicates
therefore read the whole window record, not just the process image.
"""

from __future__ import annotations

import ctypes
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import PurePath


# Spelled with plain ctypes types rather than ctypes.wintypes, which cannot be
# imported off Windows; the Windows-only entry points are resolved when called.
_BOOL = ctypes.c_int
_DWORD = ctypes.c_ulong
_HWND = ctypes.c_void_p
_LPARAM = ctypes.c_ssize_t

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_IMAGE_PATH_CHARACTERS = 1024
_TEXT_CHARACTERS = 512


@dataclass(frozen=True)
class DesktopWindow:
    class_name: str
    title: str
    visible: bool
    process_id: int
    process_image: str

    def steals_focus(self) -> bool:
        """A titled, visible top-level window is one a person would see."""

        return self.visible and bool(self.title)

    def mentions(self, token: str) -> bool:
        """Report whether the title or the owning image names `token`."""

        folded = token.lower()
        return folded in self.title.lower() or folded in self.process_image.lower()


def is_inno_setup_image(image: str) -> bool:
    """Inno Setup runs setup and uninstall from an `is-*.tmp` extraction directory."""

    return any(
        part.lower().startswith("is-") and part.lower().endswith(".tmp")
        for part in PurePath(image).parts
    )


def _process_image(kernel32: "ctypes.WinDLL", process_id: int) -> str:
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, process_id
    )
    if not handle:
        return ""
    try:
        size = _DWORD(_IMAGE_PATH_CHARACTERS)
        buffer = ctypes.create_unicode_buffer(_IMAGE_PATH_CHARACTERS)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def sample_windows(
    window_predicate: Callable[[DesktopWindow], bool],
    *,
    image_cache: dict[int, str] | None = None,
) -> list[DesktopWindow]:
    """Enumerate top-level windows the predicate accepts.

    `image_cache` memoises process image lookups for one sampling session; the
    desktop carries enough top-level windows that resolving each one on every
    sample dominates the cost.
    """

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enumerate_windows = ctypes.WINFUNCTYPE(_BOOL, _HWND, _LPARAM)
    cache = {} if image_cache is None else image_cache
    found: list[DesktopWindow] = []

    def visit(window_handle: int, _parameter: int) -> bool:
        process_id = _DWORD()
        user32.GetWindowThreadProcessId(window_handle, ctypes.byref(process_id))
        if process_id.value not in cache:
            cache[process_id.value] = _process_image(kernel32, process_id.value)
        class_name = ctypes.create_unicode_buffer(_TEXT_CHARACTERS)
        user32.GetClassNameW(window_handle, class_name, _TEXT_CHARACTERS)
        title = ctypes.create_unicode_buffer(_TEXT_CHARACTERS)
        user32.GetWindowTextW(window_handle, title, _TEXT_CHARACTERS)
        window = DesktopWindow(
            class_name=class_name.value,
            title=title.value,
            visible=bool(user32.IsWindowVisible(window_handle)),
            process_id=process_id.value,
            process_image=cache[process_id.value],
        )
        if window_predicate(window):
            found.append(window)
        return True

    user32.EnumWindows(enumerate_windows(visit), 0)
    return found


@contextmanager
def recording_windows(
    window_predicate: Callable[[DesktopWindow], bool],
    *,
    interval_seconds: float = 0.1,
) -> Iterator[set[DesktopWindow]]:
    """Poll matching top-level windows for the duration of the block.

    The polled set is the evidence: a window that exists only between two
    samples still gets recorded as long as it outlives one interval, and both an
    installer progress window and a launcher console live for seconds.
    """

    observed: set[DesktopWindow] = set()
    finished = threading.Event()

    def poll() -> None:
        image_cache: dict[int, str] = {}
        while not finished.is_set():
            observed.update(sample_windows(window_predicate, image_cache=image_cache))
            time.sleep(interval_seconds)

    recorder = threading.Thread(target=poll, daemon=True)
    recorder.start()
    try:
        yield observed
    finally:
        finished.set()
        recorder.join(timeout=10)
