"""
InactivityWatchdog — Activity-based timeout architecture.

Replaces rigid wall-clock timers with inactivity-based timeouts.
Resets timeout countdown whenever active browser activity occurs:
- Navigation (page.goto, URL changes)
- Form filling / Typing (keystrokes, field values)
- File uploads (resume, attachments)
- Screenshots

Excludes mandatory pacing sleeps (e.g. 150s spacing policy, field pacing delays)
and file uploads from the timeout budget via pause/resume context managers.
"""

import asyncio
import contextlib
import logging
import time
from contextvars import ContextVar
from typing import Any, Coroutine, List, Optional

logger = logging.getLogger(__name__)

# ContextVar holding the active stack of InactivityWatchdogs for the current task/coroutine
_watchdog_stack: ContextVar[List["InactivityWatchdog"]] = ContextVar("_watchdog_stack", default=[])


class InactivityWatchdog:
    """
    Monitors a coroutine for genuine inactivity (lack of progress/events).
    Wall-clock time spent during explicit pauses (e.g. pacing delays, file uploads)
    is completely excluded from the timeout budget.
    """

    def __init__(self, timeout_seconds: float = 600.0, name: str = "InactivityWatchdog"):
        self.timeout_seconds = float(timeout_seconds)
        self.name = name
        self.last_activity_time = time.monotonic()
        self.last_activity_desc = "init"
        self._pause_count = 0
        self.is_paused = False
        self.pause_reason = ""
        self.timed_out = False
        self.timeout_details = ""
        self._target_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None

    @classmethod
    def get_active_watchdogs(cls) -> List["InactivityWatchdog"]:
        return _watchdog_stack.get()

    @classmethod
    def record_current_activity(cls, action: str = "activity") -> None:
        """Record activity across all active watchdogs in the current async context."""
        for wd in cls.get_active_watchdogs():
            wd.record_activity(action)

    @classmethod
    @contextlib.contextmanager
    def pause_current(cls, reason: str = ""):
        """Synchronously pause all active watchdogs in the current context."""
        active = cls.get_active_watchdogs()
        for wd in active:
            wd.pause(reason)
        try:
            yield
        finally:
            for wd in active:
                wd.resume(reason)

    @classmethod
    @contextlib.asynccontextmanager
    async def pause_current_async(cls, reason: str = ""):
        """Asynchronously pause all active watchdogs in the current context."""
        active = cls.get_active_watchdogs()
        for wd in active:
            wd.pause(reason)
        try:
            yield
        finally:
            for wd in active:
                wd.resume(reason)

    def record_activity(self, action: str = "activity") -> None:
        """Reset the inactivity countdown clock and record the action description."""
        self.last_activity_time = time.monotonic()
        self.last_activity_desc = action
        logger.debug(f"[{self.name}] Activity recorded: {action}")

    def pause(self, reason: str = "") -> None:
        """Pause the inactivity watchdog (e.g. during mandatory pacing delay)."""
        self._pause_count += 1
        self.is_paused = True
        self.pause_reason = reason
        logger.debug(f"[{self.name}] Paused: {reason} (depth={self._pause_count})")

    def resume(self, reason: str = "") -> None:
        """Resume the inactivity watchdog and reset the countdown clock."""
        if self._pause_count > 0:
            self._pause_count -= 1
        if self._pause_count == 0:
            self.is_paused = False
            self.last_activity_time = time.monotonic()
            self.last_activity_desc = f"resumed ({reason or self.pause_reason})"
            logger.debug(f"[{self.name}] Resumed: {reason or self.pause_reason}")

    @contextlib.contextmanager
    def paused(self, reason: str = ""):
        self.pause(reason)
        try:
            yield
        finally:
            self.resume(reason)

    async def _monitor_loop(self) -> None:
        """Polls periodically to detect if inactivity exceeds timeout_seconds."""
        interval = min(1.0, max(0.05, self.timeout_seconds / 5.0))
        while True:
            await asyncio.sleep(interval)
            if self.is_paused:
                continue
            idle_duration = time.monotonic() - self.last_activity_time
            if idle_duration >= self.timeout_seconds:
                self.timed_out = True
                self.timeout_details = (
                    f"Inactivity timeout reached ({self.timeout_seconds}s idle >= threshold). "
                    f"Last active action: '{self.last_activity_desc}'"
                )
                logger.warning(f"[{self.name}] {self.timeout_details}. Cancelling target task.")
                if self._target_task and not self._target_task.done():
                    self._target_task.cancel()
                break

    async def __aenter__(self) -> "InactivityWatchdog":
        """Support using watchdog as an async context manager: async with watchdog:"""
        self._target_task = asyncio.current_task()
        self.last_activity_time = time.monotonic()
        self.timed_out = False
        self._pause_count = 0
        self.is_paused = False

        stack = _watchdog_stack.get().copy()
        stack.append(self)
        self._token = _watchdog_stack.set(stack)

        self._monitor_task = asyncio.create_task(self._monitor_loop())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        _watchdog_stack.reset(self._token)
        if exc_type is asyncio.CancelledError and self.timed_out:
            raise asyncio.TimeoutError(self.timeout_details) from None
        return False

    async def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """
        Run the coroutine under this inactivity watchdog.
        Pushes self onto the ContextVar stack and monitors inactivity.
        """
        async with self:
            return await coro

