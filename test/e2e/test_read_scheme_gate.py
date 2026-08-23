"""E2e for the read gate's URL-scheme policy (#70 M2) through a real MCP server.

Everything here drives a live server process under a *custom* allowlist file and
talks to it over the MCP client, so the whole chain runs — YAML -> loader ->
ReadPolicy -> validate_url -> the navigate/read tools -> real headless Chrome.

The security property under test: the read policy accepts only ``https`` by
default, and a non-https scheme (``file://``, plaintext ``http://``, ``ftp://``)
is re-enabled *only* for a host the operator named explicitly in
``website_overrides`` — a blanket ``"*": [".*"]`` does not silently re-open it.
The two opt-ins have their own worked samples (file reads, localhost dev server),
so both are exercised end-to-end here, including a real read off the granted
resource.

Offline & deterministic: refusals happen in ``validate_url`` before any load;
the allowed file:// / http://localhost targets are served locally (a temp file
and an in-process ``http.server``), never the public internet.
"""
import functools
import http.server
import json
import os
import re
import sys
import threading

import pytest
import yaml

from browden.web_navigator.utils.network_utils import get_free_port


# --- server-under-a-custom-allowlist factory -------------------------------

@pytest.fixture
def make_server(tmp_path):
    """make(config: dict) -> started McpServerHarness under that allowlist.

    The dict is serialized with ``yaml.safe_dump`` (so path regexes with regex
    metacharacters are quoted correctly), each call gets its own config dir (so
    the policy under test doesn't depend on the host's ~/.browden), and every
    harness is stopped on teardown.
    """
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_harness import McpServerHarness

    started: list = []
    counter = {"n": 0}

    def make(config: dict):
        d = tmp_path / f"cfg{counter['n']}"
        counter["n"] += 1
        d.mkdir()
        cfg = d / "allowlist.yaml"
        cfg.write_text(yaml.safe_dump(config, sort_keys=False))
        # No Tranco snapshot is written (these policies keep Tranco off); the
        # loader tolerates its absence — see PolicySet._build_read_policy.
        cache = d / "cache"
        cache.mkdir()
        harness = McpServerHarness(cache, allowlist_path=cfg)
        harness.start()
        started.append(harness)
        return harness

    yield make

    for harness in started:
        harness.stop()


# --- a tiny in-process http server for the localhost dev-server case --------

@pytest.fixture
def local_http_site(tmp_path):
    """Serve a one-page site on 127.0.0.1:<free port>; yield (port, marker text)."""
    root = tmp_path / "site"
    root.mkdir()
    marker = "LOCALHOST_DEV_OK"
    (root / "index.html").write_text(f"<html><body><h1 id='hdr'>{marker}</h1></body></html>")

    port = get_free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    # Bind 127.0.0.1 so navigating http://localhost:<port>/ reaches this server.
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, marker
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


# --- MCP helpers ------------------------------------------------------------

def _text(res) -> str:
    return "".join(c.text for c in (res.content or []) if getattr(c, "type", None) == "text")


def _pages(res) -> list[dict]:
    """Flatten a list_tabs result (each tab may be its own text content)."""
    out: list[dict] = []
    for c in res.content or []:
        if getattr(c, "type", None) == "text":
            data = json.loads(c.text)
            out.extend(data if isinstance(data, list) else [data])
    return out


async def _new_tab_id(mcp) -> str:
    res = await mcp.call_tool("new_blank_tab", {})
    return json.loads(res.content[0].text)["id"]


async def _nav_msg(mcp, url: str, handle: str) -> str:
    """Lowercased text a navigate(url) produced — tool output or a raised error.

    A gate refusal surfaces the ``ValidationError`` message either as the tool's
    error text or (the SDK may re-raise a server-side error) as an exception; we
    fold both into one string so callers can key on a signature substring.
    """
    try:
        res = await mcp.call_tool("navigate", {"url": url, "id": handle})
        return _text(res).lower()
    except Exception as e:
        return str(e).lower()


def _scheme_refused(msg: str) -> bool:
    # Exact gate phrase — not a loose "scheme" match, which would collide with a
    # temp path that happens to contain the word (e.g. this test's own dir).
    return "scheme not allowed" in msg  # "URL scheme not allowed: 'file' — only https ..."


def _allowlist_refused(msg: str) -> bool:
    return "not on allowlist" in msg  # "URL not on allowlist: <host><path>"


# Opens every host over https, but names no host explicitly -> the scheme gate
# must still refuse non-https everywhere.
_WILDCARD_HTTPS = {"read": {"enabled": True, "tranco": {"enabled": False},
                            "website_overrides": {"*": [".*"]}}}


# ===========================================================================
# https-only by default
# ===========================================================================

@pytest.mark.asyncio
async def test_wildcard_https_does_not_reopen_nonhttps_schemes(make_server):
    harness = make_server(_WILDCARD_HTTPS)
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    async with sse_client(harness.url) as streams:
        async with ClientSession(*streams) as mcp:
            await mcp.initialize()
            handle = await _new_tab_id(mcp)

            # Non-https schemes are refused even under a blanket "*": [".*"].
            assert _scheme_refused(await _nav_msg(mcp, "file:///etc/passwd", handle))
            assert _scheme_refused(await _nav_msg(mcp, "http://plain-http-xyz.test/", handle))
            assert _scheme_refused(await _nav_msg(mcp, "ftp://files-xyz.test/x", handle))

            # An https target is NOT scheme-refused: the wildcard admits the host,
            # so the request reaches Chrome and fails on DNS instead (a navigation
            # outcome, not a gate refusal) — which is all we assert.
            msg = await _nav_msg(mcp, "https://made-up-host-xyz-987.test/", handle)
            assert not _scheme_refused(msg)
            assert not _allowlist_refused(msg)


# ===========================================================================
# file:// opt-in
# ===========================================================================

@pytest.mark.asyncio
async def test_file_scheme_opt_in_allows_scoped_path_and_reads_it(make_server, tmp_path):
    docroot = tmp_path / "docs"
    docroot.mkdir()
    page = docroot / "report.html"
    page.write_text("<html><body><h1 id='hdr'>FILE_READ_OK</h1></body></html>")
    secret = docroot / "secret.txt"
    secret.write_text("nope")

    # file:// URLs carry an EMPTY host; the "" key opts them in, path-scoped to
    # this one .html file (secret.txt is intentionally outside the regex).
    config = {"read": {"enabled": True, "tranco": {"enabled": False},
                       "website_overrides": {"": ["^" + re.escape(str(page)) + "$"]}}}
    harness = make_server(config)
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    async with sse_client(harness.url) as streams:
        async with ClientSession(*streams) as mcp:
            await mcp.initialize()
            handle = await _new_tab_id(mcp)

            # The allowed local file loads (no scheme/allowlist refusal) ...
            msg = await _nav_msg(mcp, f"file://{page}", handle)
            assert not _scheme_refused(msg)
            assert not _allowlist_refused(msg)
            # ... and its content actually reads back through the DOM tools.
            hdr = await mcp.call_tool("query_selector", {"css_selector": "#hdr", "id": handle})
            assert json.loads(_text(hdr))["element"]["text"] == "FILE_READ_OK"

            # A sibling path outside the regex is refused by the allowlist even
            # though file:// is now an accepted scheme (path-scope holds).
            assert _allowlist_refused(await _nav_msg(mcp, f"file://{secret}", handle))


@pytest.mark.asyncio
async def test_file_scheme_refused_without_opt_in(make_server, tmp_path):
    page = tmp_path / "x.html"
    page.write_text("<html></html>")
    harness = make_server(_WILDCARD_HTTPS)  # opens https everywhere, no "" key
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    async with sse_client(harness.url) as streams:
        async with ClientSession(*streams) as mcp:
            await mcp.initialize()
            handle = await _new_tab_id(mcp)
            assert _scheme_refused(await _nav_msg(mcp, f"file://{page}", handle))


# ===========================================================================
# localhost dev-server (plaintext http) opt-in
# ===========================================================================

@pytest.mark.asyncio
async def test_localhost_opt_in_allows_plaintext_dev_server_and_reads_it(
        make_server, local_http_site):
    port, marker = local_http_site
    config = {"read": {"enabled": True, "tranco": {"enabled": False},
                       "website_overrides": {"localhost": [".*"]}}}
    harness = make_server(config)
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    async with sse_client(harness.url) as streams:
        async with ClientSession(*streams) as mcp:
            await mcp.initialize()
            handle = await _new_tab_id(mcp)

            # http://localhost:<port>/ is opted in -> it loads and reads back.
            msg = await _nav_msg(mcp, f"http://localhost:{port}/", handle)
            assert not _scheme_refused(msg)
            assert not _allowlist_refused(msg)
            hdr = await mcp.call_tool("query_selector", {"css_selector": "#hdr", "id": handle})
            assert json.loads(_text(hdr))["element"]["text"] == marker

            # 127.0.0.1 is a DIFFERENT host that was NOT opted in, so the same
            # plaintext-http request is scheme-refused.
            assert _scheme_refused(await _nav_msg(mcp, f"http://127.0.0.1:{port}/", handle))


# ===========================================================================
# about:blank is always readable, even under a deny-all read policy
# ===========================================================================

@pytest.mark.asyncio
async def test_about_blank_is_readable_and_kept_under_deny_all(make_server):
    # enabled read policy with no overrides and Tranco off -> every site denied.
    harness = make_server({"read": {"enabled": True, "tranco": {"enabled": False}}})
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    async with sse_client(harness.url) as streams:
        async with ClientSession(*streams) as mcp:
            await mcp.initialize()
            handle = await _new_tab_id(mcp)

            # A not-yet-navigated tab (about:blank) reads fine although the policy
            # denies every site — validate_url special-cases it.
            res = await mcp.call_tool("query_selector", {"css_selector": "html", "id": handle})
            assert json.loads(_text(res)).get("found") is True

            # list_tabs (which closes non-allowlisted tabs, finding H2) keeps it.
            listing = await mcp.call_tool("list_tabs", {})
            assert handle in [p["id"] for p in _pages(listing)]


# ===========================================================================
# denylist vetoes even a wildcard-allowed host
# ===========================================================================

@pytest.mark.asyncio
async def test_denylist_vetoes_wildcard_allowed_host(make_server):
    config = {"denylist": {"blocked-xyz.test": [".*"]},
              "read": {"enabled": True, "tranco": {"enabled": False},
                       "website_overrides": {"*": [".*"]}}}
    harness = make_server(config)
    from mcp.client.sse import sse_client
    from mcp import ClientSession

    async with sse_client(harness.url) as streams:
        async with ClientSession(*streams) as mcp:
            await mcp.initialize()
            handle = await _new_tab_id(mcp)
            # https is fine scheme-wise, the wildcard would allow it, but the
            # denylist wins -> refused.
            assert _allowlist_refused(await _nav_msg(mcp, "https://blocked-xyz.test/", handle))
