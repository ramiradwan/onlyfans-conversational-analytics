"""Explicit socket-loop selection shared by Brain process entries."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable


def uvicorn_loop() -> str:
    return "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"


def server_loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    return asyncio.SelectorEventLoop if sys.platform == "win32" else None
