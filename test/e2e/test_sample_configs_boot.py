"""End-to-end: EVERY shipped sample config boots the real MCP server.

Regression guard for a whole class of bug: a write action gains a new page-rule
field (e.g. ``field_ids`` for write-text, ``keys`` for press-key) that the
*runtime* parser accepts, but the *file-load schema* (``configs/loader/schema.py``)
does not — so the config parses fine in unit tests that build ``ActionAllowlist``
from a dict, yet the deployed server refuses it on load (``unknown keys [...]``)
and the feature is dead in production.

Booting the real ``python -m browden.mcp.server`` subprocess against each shipped
sample exercises the same ``load_allowlist`` -> ``validate_allowlist_data`` ->
``ActionAllowlist`` path the live service uses on startup — the path
dict-constructed tests skip. ``main()`` turns a ``ConfigError`` into a
``parser.error`` (a non-zero exit), so a schema-rejected sample makes the process
die before it binds, and ``harness.start()`` raises.

Deliberately parametrized over ALL of ``configs/samples/*.yaml`` (not one named
file), so a FUTURE action/field whose sample skips a schema update is caught here
automatically — no need to remember to add a per-feature test.
"""
from pathlib import Path

import pytest

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "configs" / "samples"
SAMPLES = sorted(SAMPLES_DIR.glob("*.yaml"))


def test_sample_dir_is_populated():
    # Guard the parametrization itself: an empty glob would make the per-sample
    # test below vacuously "pass" (zero cases), hiding a moved/renamed dir.
    assert SAMPLES, f"no sample configs found under {SAMPLES_DIR}"


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda p: p.name)
def test_shipped_sample_boots_server(tmp_path, sample):
    from mcp_harness import McpServerHarness

    cache = tmp_path / "cache"
    cache.mkdir()  # the harness opens its log file in here, so it must exist
    harness = McpServerHarness(cache, allowlist_path=sample)

    # Raises "MCP server died during startup ..." if `sample` fails schema
    # validation on load — the exact production failure this guards against.
    harness.start()
    harness.stop()
