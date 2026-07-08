"""E2e: the read gate reduces a host to its registrable domain via the PSL.

A real server process with a real Tranco snapshot + Public Suffix List beside its
config, driven through the MCP ``navigate`` tool. Proves finding H1 end-to-end:
an attacker-controlled subdomain of a public suffix that is itself in Tranco
(``github.io``) is refused, and a lookalike is refused, while a subdomain of a
listed registrable domain is not.

Offline & deterministic: the refusals happen in ``validate_url`` before any page
load, and the one allowed navigation targets a made-up listed domain (DNS fails
fast) so the test never depends on a live external site.
"""
import gzip
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

# The snapshot lists a real public suffix (github.io), a made-up registrable
# domain we can navigate to without hitting the network, and google.com.
_LISTED = ["google.com", "github.io", "example-listed-xyz.com"]


def _psl_config_dir(tmp_path: Path) -> Path:
    """A config dir with allowlist.yaml (Tranco on), a Tranco snapshot of
    ``_LISTED``, and publicsuffix2's bundled PSL (offline; has github.io) beside
    them — exactly the layout the read gate loads from a config dir."""
    import publicsuffix2

    d = tmp_path / "psl_cfg"
    d.mkdir()
    (d / "allowlist.yaml").write_text(
        "read:\n  enabled: true\n  tranco: {enabled: true, top_n: 500000}\n")
    with gzip.open(d / "tranco-top-400k.txt.gz", "wt", encoding="utf-8") as fh:
        fh.write("\n".join(_LISTED) + "\n")
    shutil.copy(Path(publicsuffix2.__file__).resolve().parent / "public_suffix_list.dat",
                d / "public_suffix_list.dat")
    return d


@pytest.fixture
def psl_mcp_server(tmp_path):
    """A fresh MCP server whose read gate uses the PSL config dir above."""
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    cfg = _psl_config_dir(tmp_path)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    harness = McpServerHarness(cache_dir, allowlist_path=cfg / "allowlist.yaml")
    harness.start()
    yield harness
    harness.stop()


async def _new_tab_id(mcp) -> str:
    res = await mcp.call_tool("new_blank_tab", {})
    return json.loads(res.content[0].text)["id"]


def _text(res) -> str:
    return "".join(c.text for c in (res.content or []) if getattr(c, "type", None) == "text")


async def _navigate_refused(mcp, url, tab_id) -> bool:
    """True iff the READ GATE refused ``url``.

    The gate's signature is the ``... not on allowlist ...`` ValidationError (in
    the tool's error text, or a raised exception — the SDK surfaces server-side
    errors either way). A mere page-load failure (a made-up domain not resolving)
    also sets ``isError`` but carries a network message, so we key on the
    ``allowlist`` signature, never ``isError`` alone."""
    try:
        res = await mcp.call_tool("navigate", {"url": url, "id": tab_id})
        return "allowlist" in _text(res).lower()
    except Exception as e:  # SDK may raise on a server-side ValidationError
        return "allowlist" in str(e).lower()


@pytest.mark.asyncio
async def test_psl_refuses_public_suffix_and_lookalike_subdomains(psl_mcp_server, mcp_client_session):
    async with mcp_client_session(psl_mcp_server) as mcp:
        tab_id = await _new_tab_id(mcp)
        # github.io is a PUBLIC SUFFIX in Tranco; an arbitrary Pages subdomain
        # reduces to itself (not github.io) and is unlisted -> refused.
        assert await _navigate_refused(mcp, "https://nope-xyz-987.github.io/", tab_id)
        # A suffix-spoof lookalike reduces to evil-xyz.test, not google.com.
        assert await _navigate_refused(mcp, "https://google.com.evil-xyz.test/", tab_id)


@pytest.mark.asyncio
async def test_psl_allows_subdomain_of_listed_registrable_domain(psl_mcp_server, mcp_client_session):
    async with mcp_client_session(psl_mcp_server) as mcp:
        tab_id = await _new_tab_id(mcp)
        # sub.example-listed-xyz.com reduces (via the PSL) to the listed
        # example-listed-xyz.com, so the gate must NOT refuse it. The load itself
        # fails (the domain doesn't resolve) — that is a navigation outcome, not a
        # read-gate refusal, which is all we assert.
        assert not await _navigate_refused(mcp, "https://sub.example-listed-xyz.com/", tab_id)
