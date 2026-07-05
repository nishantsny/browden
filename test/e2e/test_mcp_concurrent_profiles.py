"""Two concurrent MCP clients on two Chrome profiles, through the real server.

Stays offline like the rest of the e2e suite (no external hosts — a live site
can bot-wall or consent-redirect a CI runner): tabs are opened without a URL,
which is enough to prove profile isolation, id namespacing, and routing. No
exact tab counts either — the harness server is shared session-wide, so other
tests may own tabs in the default profile.
"""
import asyncio
import json

import pytest


def _pages_from(result) -> list[dict]:
    tabs = []
    for c in result.content:
        if c.type == "text":
            data = json.loads(c.text)
            if isinstance(data, list):
                tabs.extend(data)
            else:
                tabs.append(data)
    return tabs


@pytest.mark.asyncio
async def test_concurrent_profiles_are_isolated(mcp_server, mcp_client_session, tmp_path):
    async with mcp_client_session(mcp_server) as session1, \
               mcp_client_session(mcp_server) as session2:

        # Resolved, because the server canonicalizes profile paths the same way.
        profile2_dir = str((tmp_path / "profile2").resolve())

        # 1. Concurrently create new tabs in distinct profiles (default + profile2).
        results = await asyncio.gather(
            session1.call_tool("new_blank_tab", arguments={}),
            session2.call_tool("new_blank_tab", arguments={"profile_dir": profile2_dir})
        )
        p1 = json.loads(results[0].content[0].text)
        p2 = json.loads(results[1].content[0].text)

        # Each tab is tagged with its profile; ids are namespaced per profile.
        assert p2["profile_dir"] == profile2_dir
        assert p1["profile_dir"] != p2["profile_dir"]
        id1, id2 = p1["tab_id"], p2["tab_id"]
        d1 = id1.partition("-")[0]
        d2 = id2.partition("-")[0]
        assert d1 != d2
        assert len(d1) == 8
        assert len(d2) == 8

        # 2. list_tabs aggregates both profiles and routes ids back to them.
        tabs = _pages_from(await session1.call_tool("list_tabs", arguments={}))
        by_id = {p["tab_id"]: p for p in tabs}
        assert id1 in by_id and id2 in by_id
        assert by_id[id2]["profile_dir"] == profile2_dir
        assert by_id[id1]["profile_dir"] != profile2_dir

        # 3. Concurrent DOM reads, one per profile, routed by tab_id alone.
        dom_results = await asyncio.gather(
            session1.call_tool("query_selector", arguments={"css_selector": "body", "tab_id": id1}),
            session2.call_tool("query_selector", arguments={"css_selector": "body", "tab_id": id2})
        )
        body1 = json.loads(dom_results[0].content[0].text)
        body2 = json.loads(dom_results[1].content[0].text)
        assert body1.get("found") is True
        assert body2.get("found") is True
        assert body1["element"]["tag"] == "body"
        assert body2["element"]["tag"] == "body"

        # 4. Garbage tab_id returns the error envelope, not a crash.
        garbage = await session1.call_tool(
            "query_selector", arguments={"css_selector": "body", "tab_id": "deadbeef-123"})
        err = json.loads(garbage.content[0].text)
        assert "error" in err
        assert err["tab_id"] == "deadbeef-123"
