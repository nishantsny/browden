import asyncio
import json
from urllib.parse import urlparse

import pytest


@pytest.mark.asyncio
async def test_concurrent_profiles_are_isolated(mcp_server, mcp_client_session, tmp_path):
    # Two client sessions to simulate two clients or concurrent tool calls from one client
    # but since mcp_client_session yields an already-initialized session, we can just use
    # it to fire concurrent requests.
    async with mcp_client_session(mcp_server) as session1, \
               mcp_client_session(mcp_server) as session2:
        
        profile2_dir = str(tmp_path / "profile2")
        
        # 1. Concurrently create new pages in distinct profiles
        results = await asyncio.gather(
            session1.call_tool("new_page", arguments={"url": "https://www.amazon.com"}),
            session2.call_tool("new_page", arguments={"url": "https://scholar.google.com", "profile_dir": profile2_dir})
        )
        
        p1 = json.loads(results[0].content[0].text)
        p2 = json.loads(results[1].content[0].text)
        
        # Check URLs via hostname to avoid bot wall flakes
        assert urlparse(p1["url"]).hostname.endswith("amazon.com")
        assert urlparse(p2["url"]).hostname.endswith("scholar.google.com")
        
        # Check that page_id prefixes (digests) are different
        id1 = p1["page_id"]
        id2 = p2["page_id"]
        d1, _, _ = id1.partition("-")
        d2, _, _ = id2.partition("-")
        assert d1 != d2
        assert len(d1) == 8
        assert len(d2) == 8
        
        # 2. list_pages shows both profiles
        lp = await session1.call_tool("list_pages", arguments={})
        pages = []
        for c in lp.content:
            if c.type == "text":
                data = json.loads(c.text)
                if isinstance(data, list):
                    pages.extend(data)
                else:
                    pages.append(data)
        assert len(pages) == 4
        
        profile_dirs = {p["profile_dir"] for p in pages}
        assert profile2_dir in profile_dirs
        
        # 3. Concurrent DOM reads
        dom_results = await asyncio.gather(
            session1.call_tool("query_selector", arguments={"css_selector": "body", "page_id": id1}),
            session2.call_tool("query_selector", arguments={"css_selector": "body", "page_id": id2})
        )
        
        body1 = json.loads(dom_results[0].content[0].text)
        body2 = json.loads(dom_results[1].content[0].text)
        assert body1.get("found") is True
        assert body2.get("found") is True
        assert body1["element"]["tag"] == "body"
        assert body2["element"]["tag"] == "body"
        
        # 4. Garbage page_id returns an envelope
        garbage = await session1.call_tool("query_selector", arguments={"css_selector": "body", "page_id": "deadbeef-123"})
        err = json.loads(garbage.content[0].text)
        assert "error" in err
        assert err["page_id"] == "deadbeef-123"
