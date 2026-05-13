"""Tracks the last-access time of each open tab so idle ones can be reaped.

Pure: no Selenium import. The clock is injectable (and ``now`` can be passed per
call) so tests drive it with a fake clock instead of sleeping. All times are
durations compared against ``time.monotonic`` — never wall-clock — so a system
clock jump can't cause a spurious reap.
"""
import time


class PageRegistry:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last_access: dict[str, float] = {}

    def touch(self, page_id: str, now: float | None = None) -> None:
        self._last_access[page_id] = self._clock() if now is None else now

    def forget(self, page_id: str) -> None:
        self._last_access.pop(page_id, None)

    def idle_pages(self, ttl: float, now: float | None = None) -> list[str]:
        current = self._clock() if now is None else now
        return [pid for pid, ts in self._last_access.items() if current - ts >= ttl]
