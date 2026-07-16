"""AllowlistRefresher: the live allowlist and its lock-free hot-reload.

The refresher owns the current ActionAllowlist and swaps in a fresh one (an
atomic attribute rebind — RCU) when the backing file changes. These exercise the
pure decision (maybe_reload) plus the async poller that drives it.
"""
import asyncio

import pytest

from browden.configs.loader.refresher import AllowlistRefresher, _stat_signature


def _write(path, *, top_n: int) -> None:
    """A minimal valid allowlist whose Tranco top_n we vary so reloads are observable."""
    path.write_text(
        "read:\n"
        f"  tranco: {{enabled: true, top_n: {top_n}}}\n"
        "  website_overrides:\n"
        '    "*": [".*"]\n'
    )


@pytest.fixture
def config(tmp_path):
    f = tmp_path / "allowlist.yaml"
    _write(f, top_n=1000)
    return f


def test_from_path_loads_and_watches(config):
    r = AllowlistRefresher.from_path(config)
    assert r.allowlist.read_policy.is_allowed("anything.test", "/")  # "*" override admits it


def test_reload_swaps_in_edited_config(config):
    r = AllowlistRefresher.from_path(config)
    before = r.allowlist
    # A different-size edit changes the (mtime, size) signature.
    config.write_text(
        "read:\n"
        "  tranco: {enabled: true, top_n: 5}\n"
        "  website_overrides:\n"
        '    "specific.test": ["^/ok"]\n'
    )
    assert r.maybe_reload() is True
    assert r.allowlist is not before                          # atomic swap happened
    assert r.allowlist.read_policy.override_has_host("specific.test")
    assert not r.allowlist.read_policy.override_has_host("anything.test")  # "*" gone


def test_unchanged_file_is_a_noop(config):
    r = AllowlistRefresher.from_path(config)
    before = r.allowlist
    assert r.maybe_reload() is False  # same signature
    assert r.allowlist is before


def test_static_refresher_watches_nothing(config):
    fixed = AllowlistRefresher.from_path(config).allowlist
    r = AllowlistRefresher.static(fixed)
    config.write_text("read:\n  enabled: false\n")  # edit ignored: no path watched
    assert r.maybe_reload() is False
    assert r.allowlist is fixed


def test_invalid_edit_keeps_last_good_and_stops_retrying(config):
    r = AllowlistRefresher.from_path(config)
    before = r.allowlist
    config.write_text("read: [unclosed\n")  # no longer valid YAML
    assert r.maybe_reload() is False   # fail-safe: not swapped
    assert r.allowlist is before       # last-good retained
    # Signature was recorded, so the same broken file isn't re-parsed next tick.
    assert r._stat == _stat_signature(config)
    assert r.maybe_reload() is False


def test_deleted_file_keeps_last_good(config):
    r = AllowlistRefresher.from_path(config)
    before = r.allowlist
    config.unlink()
    assert r.maybe_reload() is False
    assert r.allowlist is before


def test_stat_signature_none_for_missing(tmp_path):
    assert _stat_signature(tmp_path / "nope.yaml") is None


def test_run_poller_reloads_then_cancels_cleanly(config):
    r = AllowlistRefresher.from_path(config, interval=0.01)

    async def _drive() -> None:
        before = r.allowlist
        async with r.run():
            _write(config, top_n=7)  # size differs from the 1000 seed -> new signature
            for _ in range(200):     # give the poller ticks to notice
                await asyncio.sleep(0.01)
                if r.allowlist is not before:
                    break
            assert r.allowlist is not before
        # On exit the poller task is cancelled; no pending tasks leak.
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(_drive())


def test_static_run_is_a_harmless_noop(config):
    r = AllowlistRefresher.static(AllowlistRefresher.from_path(config).allowlist)

    async def _drive() -> None:
        async with r.run():           # no file watched: yields, poller finishes at once
            await asyncio.sleep(0.01)

    asyncio.run(_drive())
