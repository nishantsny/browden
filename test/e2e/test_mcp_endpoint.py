import json
import pytest

@pytest.mark.asyncio
async def test_mcp_endpoint(mcp_server, mcp_client_session):
    async with mcp_client_session(mcp_server) as mcp_client:
        # list_tools returns exactly the expected tool names.
        tools_result = await mcp_client.list_tools()
        tools = [t.name for t in tools_result.tools]
        expected_tools = {
            "list_profiles", "list_tabs", "new_blank_tab", "close_tab", "select_tab", "navigate",
            "click", "insert_text", "press_key", "upload_file", "get_element_by_id",
            "get_elements_by_class_name", "query_selector", "query_selector_all",
            "screenshot", "force_reload_tab", "invalidate_dom_cache",
            "switch_to_frame", "switch_to_parent_frame", "switch_to_default_content",
        }
        assert set(tools) == expected_tools
    
        # new_blank_tab() -> blank tab -> returns an id; list_tabs includes it.
        new_page_result = await mcp_client.call_tool("new_blank_tab", {})
        new_page_data = json.loads(new_page_result.content[0].text)
        
        handle = new_page_data.get("id")
        assert handle is not None
    
        list_pages_result = await mcp_client.call_tool("list_tabs", {})
        list_pages_items = []
        for c in list_pages_result.content:
            if c.type == "text":
                try:
                    data = json.loads(c.text)
                    if isinstance(data, list):
                        list_pages_items.extend(data)
                    else:
                        list_pages_items.append(data)
                except Exception:
                    pass
            
        assert any(p.get("id") == handle for p in list_pages_items)
    
        # query_selector("html") on the new page -> found (about:blank still has a root element).
        query_res = await mcp_client.call_tool("query_selector", {"css_selector": "html", "id": handle})
        query_data = json.loads(query_res.content[0].text)
        assert query_data.get("found") is True
    
        # navigate to a data: URL -> MCP tool error (validate_url reads it as a host named "data", which is on
        # no allowlist) -> proves the validator is live, and that the refusal's reason reaches the client and
        # not only the fact of an error.
        nav_res = await mcp_client.call_tool("navigate", {"url": "data:text/html,<h1>Hello</h1>", "id": handle})
        assert nav_res.is_error is True
        assert "not on allowlist" in nav_res.content[0].text
    
        # invalidate_dom_cache drops the snapshot that query built; the next read
        # re-fetches and still answers.
        inv_res = await mcp_client.call_tool("invalidate_dom_cache", {"id": handle})
        assert json.loads(inv_res.content[0].text) == {"id": handle, "invalidated": True}
        requery_res = await mcp_client.call_tool("query_selector", {"css_selector": "html", "id": handle})
        assert json.loads(requery_res.content[0].text).get("found") is True

        # screenshot returns image content
        screenshot_res = await mcp_client.call_tool("screenshot", {"id": handle})
        assert not screenshot_res.is_error
        assert any(c.type == "image" for c in screenshot_res.content)
    
        # close_tab works
        close_res = await mcp_client.call_tool("close_tab", {"id": handle})
        assert not close_res.is_error
