"""End-to-end resource-cap enforcement against a real (headless) Chrome.

Runs the MCP server under a tiny 2-session / 2-tab-per-session allowlist (the
``mcp_server_low_caps`` fixture) so both caps can be tripped cheaply. Each cap
is checked two ways: it raises the right error when exceeded, and dropping back
under it (closing a tab, or reusing an existing profile) does *not* raise.

Cap errors surface as a normal tool return whose dict carries an ``error`` key
(not an MCP protocol error), so the assertions inspect the parsed dict.
"""
import json

import pytest


def _parse(result):
    """The single wire dict a new_blank_tab / close_tab call returns."""
    return json.loads(result.content[0].text)


def _list(result):
    """Flatten a list_tabs result's text content into a list of tab dicts."""
    items = []
    for c in result.content:
        if c.type == "text":
            data = json.loads(c.text)
            items.extend(data if isinstance(data, list) else [data])
    return items


async def _new_tab(client, **args):
    return _parse(await client.call_tool("new_blank_tab", args))


# -- per-session tab cap (max_tabs_per_session = 2) -------------------------

@pytest.mark.asyncio
async def test_tab_cap_raises_when_exceeded(mcp_server_low_caps, mcp_client_session):
    async with mcp_client_session(mcp_server_low_caps) as client:
        errors = []
        # Keep opening tabs in the one (default) session past the cap of 2.
        for _ in range(5):
            data = await _new_tab(client)
            if "error" in data:
                errors.append(data)
        # At least one open was refused, and every refusal names the tab cap.
        assert errors, "expected the tab cap to reject an open"
        assert all("tabs reached" in e["error"] for e in errors)
        # The live tab count never exceeds the cap.
        assert len(_list(await client.call_tool("list_tabs", {}))) == 2


@pytest.mark.asyncio
async def test_tab_cap_frees_a_slot_when_a_tab_closes(mcp_server_low_caps, mcp_client_session):
    async with mcp_client_session(mcp_server_low_caps) as client:
        # Fill the session up to its tab cap.
        while "error" not in (data := await _new_tab(client)):
            pass
        assert "tabs reached" in data["error"]  # the fill stopped at the cap
        # Still at the cap -> opening again is refused.
        refused = await _new_tab(client)
        assert "tabs reached" in refused.get("error", "")

        # Close one tab: the live count drops back under the cap.
        victim = _list(await client.call_tool("list_tabs", {}))[0]["id"]
        closed = _parse(await client.call_tool("close_tab", {"id": victim}))
        assert closed.get("closed") == victim

        # Opening is allowed again -> no exception, a fresh tab comes back.
        reopened = await _new_tab(client)
        assert "error" not in reopened
        assert reopened.get("id")


# -- browser-session cap (max_browser_sessions = 2) -------------------------

@pytest.mark.asyncio
async def test_session_cap_raises_for_a_new_profile_beyond_the_limit(
        mcp_server_low_caps, mcp_client_session, tmp_path):
    async with mcp_client_session(mcp_server_low_caps) as client:
        # Two distinct profiles -> two independent Chrome sessions, both fine.
        a = await _new_tab(client, profile_dir=str(tmp_path / "profA"))
        b = await _new_tab(client, profile_dir=str(tmp_path / "profB"))
        assert "error" not in a
        assert "error" not in b
        # A third distinct profile is over the 2-session cap and is refused
        # before any Chrome is launched for it.
        c = await _new_tab(client, profile_dir=str(tmp_path / "profC"))
        assert "new browser session" in c.get("error", "")
        assert "limit of 2 reached" in c["error"]


@pytest.mark.asyncio
async def test_reusing_a_profile_does_not_consume_a_session_slot(
        mcp_server_low_caps, mcp_client_session, tmp_path):
    async with mcp_client_session(mcp_server_low_caps) as client:
        a, b = str(tmp_path / "profA"), str(tmp_path / "profB")
        # Open profile A (session 1).
        first = await _new_tab(client, profile_dir=a)
        assert "error" not in first
        # Ask for profile A again: it reuses session 1, so it must NOT raise the
        # *session* cap. (It may hit A's own tab cap — a different error.)
        repeat = await _new_tab(client, profile_dir=a)
        assert "new browser session" not in repeat.get("error", "")
        # A genuinely new profile B still fits as session 2 — which proves the
        # repeat above didn't burn a session slot.
        second = await _new_tab(client, profile_dir=b)
        assert "error" not in second
