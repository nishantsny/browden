"""The live runtime configuration: one loaded ``BrowdenRuntimeConfiguration``
plus periodic hot-reload.

The MCP server holds a single :class:`RuntimeConfigurationRefresher`. It owns the
current ``BrowdenRuntimeConfiguration`` and, while the server runs, re-stats the
backing config file every few seconds and swaps in a freshly-loaded one when the file changes — so
an operator can tighten or loosen the gate without restarting the process.

Lock-free by RCU / atomic pointer swap: a ``BrowdenRuntimeConfiguration`` is
immutable after construction, and every reader (the tool layer) only ever *reads*
``.runtime_configuration`` and hands that frozen object to the stateless
validator functions. A reload never mutates in place — it builds a fresh
configuration off to the side and rebinds ``self._runtime_configuration``, a
single GIL-atomic attribute store.
No reader observes a torn policy; a call already in flight simply finishes
against the reference it read, so the swap needs no lock. That holds only
because every tool reads the configuration **once per request** and decides the
whole request against that one reference — a tool that read it twice, with an
``await`` in between, could be judged half by the old rules and half by the new
(see docs/design/write-gate-atomicity.md). Because the poller
runs as an ``asyncio`` task on the *same* event loop as the tools, the rebind
can't even interleave with a tool mid-statement.
"""
import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from ...common.logger import logger
from ...mcp.validator.runtime_configuration import BrowdenRuntimeConfiguration
from .loader import load_runtime_configuration
from .schema import ConfigError

# How often the poller re-stats the config file to hot-reload it.
DEFAULT_RELOAD_INTERVAL_SECONDS = 10


def _stat_signature(path: Path) -> tuple[float, int] | None:
    """The change signature ``(st_mtime, st_size)`` of ``path``, or None if it can't be stat'd.

    Pairing size with mtime catches a same-second edit that leaves the file the
    same length but changed content — pure mtime can alias those on coarse
    filesystem timestamp resolution.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


class RuntimeConfigurationRefresher:
    """Holds the live ``BrowdenRuntimeConfiguration``; hot-reloads it from its file.

    Build one with :meth:`from_path` to watch a file, or :meth:`static` to pin a
    fixed configuration (the import-time default and unit tests, which watch
    nothing). Read the current policy off :attr:`runtime_configuration`; call :meth:`run` (an async
    context manager) to poll for changes for the server's lifetime.
    """

    def __init__(self, runtime_configuration: BrowdenRuntimeConfiguration, *,
                 path: Path | None, interval: float = DEFAULT_RELOAD_INTERVAL_SECONDS):
        self._runtime_configuration = runtime_configuration
        self._path = path
        self._interval = interval
        # The signature of the file we just loaded; None for a static refresher.
        self._stat = _stat_signature(path) if path is not None else None

    @classmethod
    def from_path(cls, path: Path | str, *,
                  interval: float = DEFAULT_RELOAD_INTERVAL_SECONDS) -> "RuntimeConfigurationRefresher":
        """Load ``path`` now and return a refresher that watches it for edits."""
        path = Path(path).expanduser()
        return cls(load_runtime_configuration(path), path=path, interval=interval)

    @classmethod
    def static(cls, runtime_configuration: BrowdenRuntimeConfiguration
               ) -> "RuntimeConfigurationRefresher":
        """A refresher pinned to a fixed configuration — watches no file, never reloads."""
        return cls(runtime_configuration, path=None)

    @property
    def runtime_configuration(self) -> BrowdenRuntimeConfiguration:
        """The current policy. Reads the atomically-swapped reference (see module docstring)."""
        return self._runtime_configuration

    def maybe_reload(self) -> bool:
        """Hot-reload iff the watched config file's ``(mtime, size)`` signature changed.

        Fail-safe by design: a vanished or unreadable file, or a config that no
        longer parses (a mid-edit save, invalid YAML), leaves the last-good
        configuration in place — a security gate must never fall open or crash on
        a bad edit. Returns True only when a fresh configuration was actually
        swapped in.
        """
        if self._path is None:
            return False
        sig = _stat_signature(self._path)
        if sig is None or sig == self._stat:
            return False  # gone/unreadable, or unchanged since last load — nothing to do
        try:
            new_runtime_configuration = load_runtime_configuration(self._path)
        except ConfigError as e:
            # Record the signature so we don't re-parse the same broken file
            # every tick; the operator's next real edit changes it and we retry.
            self._stat = sig
            logger.warning(f"allowlist reload skipped, keeping last-good config: {e}")
            return False
        self._runtime_configuration = new_runtime_configuration  # atomic RCU swap: readers see old-or-new, never torn
        self._stat = sig
        logger.info(f"Reloaded allowlist config from {self._path}")
        return True

    async def _poll_loop(self) -> None:
        """Poll the watched config every ``interval`` and hot-reload on change."""
        if self._path is None:
            return  # static refresher: nothing to watch
        while True:
            await asyncio.sleep(self._interval)
            try:
                self.maybe_reload()
            except Exception as e:  # a single bad tick must never kill the poller
                logger.warning(f"allowlist reload tick failed: {e}")

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Run the hot-reload poller for the duration of the ``async with`` block.

        Started on entry, cancelled cleanly on exit. A static refresher (no file)
        yields immediately with the poller task already finished — a harmless no-op.
        """
        task = asyncio.create_task(self._poll_loop())
        if self._path is not None:
            logger.info(f"Allowlist hot-reload poller started "
                        f"({self._interval}s interval, watching {self._path})")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
