"""A ``[ n/total ]`` completion counter for the e2e suite.

The e2e suite is slow — every test drives a real Chrome — and the release
script runs it with ``-q``, whose bare dot-per-test tells a maintainer watching
a deploy nothing about how much is left. Twenty seconds of silence after a dot
could be a slow test or a hung one, and the dots give no way to tell.

This appends a counter to each dot instead of replacing it::

    .[ 1/23]
    .[ 2/23]
    F[ 3/23]

pytest's own status character is deliberately left alone. It is the outcome
(``.`` pass, ``F`` fail, ``s`` skip) and the counter is the position; keeping
both means a failure stays visible at the exact point it happened, and the
stats pytest accumulates for its end-of-run summary are never touched. That
also keeps this plugin out of ``pytest_report_teststatus``, where overriding
the reported category would risk mis-counting xfail/xpass in the summary.

The counter is written on the ``teardown`` report because that phase fires
exactly once per test whatever the outcome — counting ``call`` instead would
silently skip tests that error or skip during setup, and the total would never
be reached.
"""
import pytest


def format_progress(completed: int, total: int) -> str:
    """Render the counter, right-aligning ``completed`` to the width of ``total``.

    Padding keeps the counter a fixed width for the whole run, so the column
    doesn't jitter when the count rolls 9 -> 10 and the output stays readable
    as a column when scanning a deploy log.
    """
    return f"[{completed:>{len(str(total))}}/{total}]"


class E2EProgress:
    """Counts completed tests and writes the counter after each status char."""

    def __init__(self, config) -> None:
        self._config = config
        self.total = 0
        self.completed = 0

    @property
    def _terminal_reporter(self):
        """Resolve the terminal reporter lazily, on first write.

        It is NOT registered yet when a conftest's ``pytest_configure`` runs, so
        looking it up at registration time would find nothing and silently
        disable the counter for every normal run. By the time reports are being
        logged it is always in place; a ``None`` here therefore means the run
        genuinely has no terminal (``-p no:terminal``) and the counter is
        skipped rather than crashing the suite over a progress display.
        """
        return self._config.pluginmanager.getplugin("terminalreporter")

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items) -> None:
        # trylast: read the item list only after every other plugin has had its
        # chance to deselect (-k, -m, --deselect), so the denominator is what
        # will actually run rather than what was collected off disk.
        self.total = len(items)

    def pytest_runtest_logreport(self, report) -> None:
        if report.when != "teardown":
            return
        self.completed += 1
        terminal_reporter = self._terminal_reporter
        if terminal_reporter is None:
            return
        terminal_reporter.write(format_progress(self.completed, self.total))
        terminal_reporter.write("\n")


def register(config) -> None:
    """Install the counter under the name ``e2e-progress``."""
    config.pluginmanager.register(E2EProgress(config), "e2e-progress")
