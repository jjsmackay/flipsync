"""Idle-timer VRAM unloader.

Releases the service's model from VRAM after a configurable idle period so it
does not squat on GPU memory that a later pipeline stage (in another container)
needs. The orchestrator's global GPU lock serialises *execution* but does
nothing about *residency*; this closes that gap.

The class is pure state + decision logic with an injectable clock so it can be
unit-tested without asyncio, sleeps, or torch. The wiring in ``main.py`` runs
the actual free on the same single-worker executor the jobs use, which
guarantees an unload can never overlap a running job.

Concurrency contract:
  - ``on_submit`` / ``on_finish`` are called around each job (event loop).
  - ``should_unload`` is the watcher-side gate (has it been quiet long enough?).
  - ``perform_unload`` runs on the job executor and re-checks that nothing is
    active before freeing, so a job arriving in the gap cancels the unload
    rather than losing its model mid-flight.
All three take a lock, so cross-thread access to the counters is safe.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


DEFAULT_IDLE_SECS = 60.0


def parse_idle_secs(raw: str | None, default: float = DEFAULT_IDLE_SECS) -> float:
    """Parse the ``IDLE_UNLOAD_SECS`` env value. Missing/blank/non-numeric falls
    back to ``default``; a valid number (including 0 or negative, which the
    caller treats as "disabled") is returned as-is."""
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class IdleUnloader:
    def __init__(
        self,
        idle_secs: float,
        is_loaded: Callable[[], bool],
        unload: Callable[[], None],
        now: Callable[[], float] = time.monotonic,
    ):
        self._idle_secs = idle_secs
        self._is_loaded = is_loaded
        self._unload = unload
        self._now = now
        self._lock = threading.Lock()
        self._active = 0
        self._last_active = now()

    def on_submit(self) -> None:
        """A job has been accepted. Counted from submit (not job start) so a
        queued job blocks unloading."""
        with self._lock:
            self._active += 1
            self._last_active = self._now()

    def on_finish(self) -> None:
        """A job finished; restart the idle clock."""
        with self._lock:
            self._active = max(0, self._active - 1)
            self._last_active = self._now()

    def should_unload(self) -> bool:
        """Watcher gate: nothing active, model resident, idle window elapsed."""
        with self._lock:
            return self._idle_and_loaded()

    def perform_unload(self) -> bool:
        """Run on the job executor. Re-check under the lock, then free. Returns
        True if it actually unloaded."""
        with self._lock:
            if self._active != 0 or not self._is_loaded():
                return False
            self._unload()
            return True

    def _idle_and_loaded(self) -> bool:
        if self._active != 0 or not self._is_loaded():
            return False
        return (self._now() - self._last_active) >= self._idle_secs
