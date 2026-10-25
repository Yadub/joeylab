"""Time limits for work that can hang in native code: Apple Vision, and models on the graphics
processor (MPS).

Python can't stop a call that's stuck inside a framework, so the work runs on daemon threads and the
caller stops waiting once the limit has passed. The update carries on without it, and a stuck thread
never keeps JoeyLab from quitting.
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout


class TimedOut(RuntimeError):
    """The work didn't finish in time (it may still be stuck on its thread)."""


def _run(fut: Future, fn, args) -> None:
    if not fut.set_running_or_notify_cancel():
        return
    try:
        fut.set_result(fn(*args))
    except BaseException as e:  # handed to whoever waits on the future
        fut.set_exception(e)


def call(fn, *args, timeout: float, what: str = "The work"):
    """fn(*args), or TimedOut after `timeout` seconds."""
    fut: Future = Future()
    threading.Thread(target=_run, args=(fut, fn, args), name="deadline", daemon=True).start()
    try:
        return fut.result(timeout=timeout)
    except FutureTimeout:
        raise TimedOut(f"{what} didn't finish in {timeout:.0f} s") from None


class Pool:
    """A few daemon worker threads, used like a ThreadPoolExecutor. Closing it drops work that
    hasn't started and lets idle workers go; one stuck on a call is simply left behind."""

    def __init__(self, workers: int, name: str = "pool"):
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._n = workers
        self._futures: list[Future] = []
        for i in range(workers):
            threading.Thread(target=self._work, name=f"{name}-{i}", daemon=True).start()

    def _work(self) -> None:
        while (item := self._q.get()) is not None:
            _run(*item)

    def submit(self, fn, *args) -> Future:
        fut: Future = Future()
        self._futures = [f for f in self._futures if not f.done()] + [fut]
        self._q.put((fut, fn, args))
        return fut

    def close(self) -> None:
        for f in self._futures:
            f.cancel()
        for _ in range(self._n):
            self._q.put(None)

    def __enter__(self) -> "Pool":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
