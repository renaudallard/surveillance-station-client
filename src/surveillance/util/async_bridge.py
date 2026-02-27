# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Bridge between GTK4 main loop and asyncio.

Runs a dedicated asyncio event loop in a background thread.
Coroutines are submitted to it, and callbacks are dispatched
back to the GTK main thread via GLib.idle_add().
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import concurrent.futures.thread
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from gi.repository import GLib  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None


def setup_async() -> asyncio.AbstractEventLoop:
    """Start a background asyncio event loop thread.

    Must be called once at startup (from the GTK main thread).
    Returns the event loop (running in the background thread).
    """
    global _loop, _thread

    if _loop is not None and _loop.is_running():
        return _loop

    _loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    _thread = threading.Thread(target=_run_loop, daemon=True, name="asyncio-bridge")
    _thread.start()

    return _loop


def get_loop() -> asyncio.AbstractEventLoop:
    """Get the background asyncio event loop."""
    global _loop
    if _loop is None or not _loop.is_running():
        _loop = setup_async()
    return _loop


def shutdown_async() -> None:
    """Stop the background event loop."""
    global _loop, _thread
    if _loop is None:
        return
    loop = _loop
    loop.call_soon_threadsafe(loop.stop)
    if _thread is not None:
        _thread.join(timeout=2)
    _loop = None
    _thread = None
    # Prevent atexit hang on leftover executor threads.
    # The process is exiting. The OS reclaims all resources.
    concurrent.futures.thread._threads_queues.clear()  # type: ignore[attr-defined]


def run_async(
    coro: Coroutine[Any, Any, T],
    callback: Any | None = None,
    error_callback: Any | None = None,
) -> concurrent.futures.Future[T]:
    """Submit an async coroutine to the background loop.

    The callback receives the result value on the GTK main thread.
    The error_callback receives the exception on the GTK main thread.
    """
    loop = get_loop()

    future = asyncio.run_coroutine_threadsafe(coro, loop)

    if callback or error_callback:

        def _on_done(f: concurrent.futures.Future[T]) -> None:
            exc = f.exception()
            if exc:
                if error_callback:
                    GLib.idle_add(error_callback, exc)
                else:
                    log.error("Async task failed: %s", exc)
            elif callback:
                GLib.idle_add(callback, f.result())

        future.add_done_callback(_on_done)

    return future
