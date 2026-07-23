"""Unit tests for test/e2e/progress.py — the e2e suite's completion counter.

Pure reporting logic only: the hooks are driven directly with stub report and
terminal-reporter objects, so no browser and no nested pytest run is involved.
"""
import sys
from pathlib import Path


E2E_DIR = Path(__file__).resolve().parents[1] / "e2e"
sys.path.insert(0, str(E2E_DIR))

import progress  # noqa: E402


class FakeReport:
    """Stands in for a pytest TestReport — only ``when`` is read."""

    def __init__(self, when: str) -> None:
        self.when = when


class FakeTerminalReporter:
    def __init__(self) -> None:
        self.written = []

    def write(self, text: str) -> None:
        self.written.append(text)

    @property
    def text(self) -> str:
        return "".join(self.written)


class FakePluginManager:
    def __init__(self, terminal_reporter) -> None:
        self.terminal_reporter = terminal_reporter
        self.registered = []

    def getplugin(self, name):
        return self.terminal_reporter if name == "terminalreporter" else None

    def register(self, plugin, name):
        self.registered.append((plugin, name))


class FakeConfig:
    def __init__(self, terminal_reporter) -> None:
        self.pluginmanager = FakePluginManager(terminal_reporter)


# -- format_progress ---------------------------------------------------------

def test_format_progress_pads_to_total_width():
    # 1 is padded to the width of 23 so the column stays fixed across the run.
    assert progress.format_progress(1, 23) == "[ 1/23]"
    assert progress.format_progress(23, 23) == "[23/23]"


def test_format_progress_handles_three_digit_totals():
    assert progress.format_progress(7, 100) == "[  7/100]"


def test_format_progress_zero_total_does_not_raise():
    # An empty selection (-k matching nothing) must not crash the reporter.
    assert progress.format_progress(0, 0) == "[0/0]"


# -- counting ----------------------------------------------------------------

def _counter(total: int):
    config = FakeConfig(FakeTerminalReporter())
    plugin = progress.E2EProgress(config)
    plugin.pytest_collection_modifyitems([object()] * total)
    return plugin, config.pluginmanager.terminal_reporter


def test_counts_once_per_test_on_teardown():
    plugin, terminal = _counter(3)
    for when in ("setup", "call", "teardown"):
        plugin.pytest_runtest_logreport(FakeReport(when))
    assert plugin.completed == 1
    assert terminal.text == "[1/3]\n"


def test_setup_and_call_reports_write_nothing():
    plugin, terminal = _counter(3)
    plugin.pytest_runtest_logreport(FakeReport("setup"))
    plugin.pytest_runtest_logreport(FakeReport("call"))
    assert plugin.completed == 0
    assert terminal.text == ""


def test_counter_reaches_total_across_several_tests():
    plugin, terminal = _counter(3)
    for _ in range(3):
        plugin.pytest_runtest_logreport(FakeReport("teardown"))
    assert plugin.completed == 3
    assert terminal.text == "[1/3]\n[2/3]\n[3/3]\n"


def test_total_reflects_deselected_items():
    # pytest hands the hook the post-deselection list, so a -k filtered run
    # counts against what actually runs, not what was collected.
    plugin, _ = _counter(10)
    plugin.pytest_collection_modifyitems([object()] * 2)
    assert plugin.total == 2


# -- terminal reporter resolution --------------------------------------------

def test_reporter_is_resolved_lazily_not_at_construction():
    """Regression guard: the reporter does not exist yet at registration time.

    ``terminalreporter`` is registered after a conftest's ``pytest_configure``
    runs, so a plugin that captured it in ``__init__`` would capture ``None``
    and silently print nothing for every real run. Resolving per write is what
    makes the counter actually appear.
    """
    config = FakeConfig(None)          # nothing registered yet, as at configure time
    plugin = progress.E2EProgress(config)
    plugin.pytest_collection_modifyitems([object()] * 2)

    config.pluginmanager.terminal_reporter = FakeTerminalReporter()   # pytest registers it
    plugin.pytest_runtest_logreport(FakeReport("teardown"))

    assert config.pluginmanager.terminal_reporter.text == "[1/2]\n"


def test_writes_are_skipped_when_there_is_no_terminal_reporter():
    # -p no:terminal leaves nothing to write through; the run must still work,
    # and the count must still advance.
    config = FakeConfig(None)
    plugin = progress.E2EProgress(config)
    plugin.pytest_collection_modifyitems([object()])
    plugin.pytest_runtest_logreport(FakeReport("teardown"))
    assert plugin.completed == 1


# -- registration ------------------------------------------------------------

def test_register_installs_the_plugin():
    config = FakeConfig(FakeTerminalReporter())
    progress.register(config)
    assert len(config.pluginmanager.registered) == 1
    plugin, name = config.pluginmanager.registered[0]
    assert name == "e2e-progress"
    assert isinstance(plugin, progress.E2EProgress)
