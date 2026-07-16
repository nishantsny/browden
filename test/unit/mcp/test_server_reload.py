"""Allowlist hot-reload: the poller swaps _ALLOWLIST when the file changes.

The reload is an atomic rebind of an immutable ActionAllowlist (RCU), so these
tests exercise the pure decision (_maybe_reload_allowlist) plus the async
lifespan poller that drives it.
"""
import asyncio

import pytest

import browden.mcp.server as server


def _write(path, tranco_top_n: int) -> None:
    """A minimal valid allowlist whose Tranco top_n we vary so reloads are observable."""
    path.write_text(
        "read:\n"
        f"  tranco: {{enabled: true, top_n: {tranco_top_n}}}\n"
        "  website_overrides:\n"
        '    "*": [".*"]\n'
    )


@pytest.fixture
def watched(tmp_path, monkeypatch):
    """Point the module's reload state at a fresh config file and load it once."""
    f = tmp_path / "allowlist.yaml"
    _write(f, 1000)
    al = server.load_allowlist(f)
    monkeypatch.setattr(server, "_ALLOWLIST", al)
    monkeypatch.setattr(server, "_ALLOWLIST_PATH", f)
    monkeypatch.setattr(server, "_ALLOWLIST_STAT", server._stat_signature(f))
    return f


def test_reload_swaps_in_edited_config(watched):
    before = server._ALLOWLIST
    # A different-size edit changes the (mtime, size) signature.
    watched.write_text(
        "read:\n"
        "  tranco: {enabled: true, top_n: 5}\n"
        "  website_overrides:\n"
        '    "specific.test": ["^/ok"]\n'
    )
    assert server._maybe_reload_allowlist() is True
    assert server._ALLOWLIST is not before          # atomic swap happened
    # The new policy is in force: the "*" override is gone, only specific.test is.
    assert server._ALLOWLIST.read_policy.override_has_host("specific.test")
    assert not server._ALLOWLIST.read_policy.override_has_host("anything.test")


def test_unchanged_file_is_a_noop(watched):
    before = server._ALLOWLIST
    assert server._maybe_reload_allowlist() is False  # same signature
    assert server._ALLOWLIST is before


def test_no_path_is_a_noop(monkeypatch):
    monkeypatch.setattr(server, "_ALLOWLIST_PATH", None)
    assert server._maybe_reload_allowlist() is False


def test_invalid_edit_keeps_last_good_and_stops_retrying(watched):
    before = server._ALLOWLIST
    watched.write_text("read: [unclosed\n")  # no longer valid YAML
    assert server._maybe_reload_allowlist() is False   # fail-safe: not swapped
    assert server._ALLOWLIST is before                 # last-good retained
    # Signature was recorded, so an unchanged broken file is not re-parsed.
    assert server._ALLOWLIST_STAT == server._stat_signature(watched)
    assert server._maybe_reload_allowlist() is False


def test_deleted_file_keeps_last_good(watched):
    before = server._ALLOWLIST
    watched.unlink()
    assert server._maybe_reload_allowlist() is False
    assert server._ALLOWLIST is before


def test_stat_signature_none_for_missing(tmp_path):
    assert server._stat_signature(tmp_path / "nope.yaml") is None


def test_lifespan_poller_reloads_then_cancels_cleanly(watched, monkeypatch):
    monkeypatch.setattr(server, "_RELOAD_INTERVAL_SECONDS", 0.01)

    async def _run() -> None:
        before = server._ALLOWLIST
        async with server._allowlist_lifespan(server.mcp):
            _write(watched, 7)  # size differs from the 1000 seed -> new signature
            for _ in range(200):               # give the poller ticks to notice
                await asyncio.sleep(0.01)
                if server._ALLOWLIST is not before:
                    break
            assert server._ALLOWLIST is not before
        # On exit the poller task is cancelled; no pending tasks leak.
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(_run())
