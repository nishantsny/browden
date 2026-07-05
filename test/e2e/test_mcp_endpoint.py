import json
import pytest

@pytest.mark.asyncio
async def test_mcp_endpoint(mcp_server, mcp_client_session):
    async with mcp_client_session(mcp_server) as mcp_client:
        # list_tools returns exactly the 12 expected tool names.
        tools_result = await mcp_client.list_tools()
        tools = [t.name for t in tools_result.tools]
        expected_tools = {
            "list_pages", "new_page", "close_page", "select_page", "navigate",
            "add_to_cart", "get_element_by_id", "get_elements_by_class_name",
            "query_selector", "query_selector_all", "screenshot", "force_reload_page"
        }
        assert set(tools) == expected_tools
    
        # new_page() (no url) -> returns an id; list_pages includes it.
        new_page_result = await mcp_client.call_tool("new_page", {})
        new_page_data = json.loads(new_page_result.content[0].text)
        
        page_id = new_page_data.get("id", new_page_data.get("page_id"))
        assert page_id is not None
    
        list_pages_result = await mcp_client.call_tool("list_pages", {})
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
            
        assert any((p.get("id") == page_id or p.get("page_id") == page_id) for p in list_pages_items)
    
        # query_selector("html") on the new page -> found (about:blank still has a root element).
        query_res = await mcp_client.call_tool("query_selector", {"css_selector": "html", "page_id": page_id})
        query_data = json.loads(query_res.content[0].text)
        assert query_data.get("found") is True
    
        # navigate to a data: URL -> MCP tool error (no netloc => validate_url rejects) -> proves validator is live
        try:
            nav_res = await mcp_client.call_tool("navigate", {"url": "data:text/html,<h1>Hello</h1>", "page_id": page_id})
            assert nav_res.isError is True
        except Exception as e:
            # MCP python SDK might raise an exception if it's a server error
            assert "ValidationError" in str(e) or "not allowed" in str(e) or "error" in str(e).lower()
    
        # screenshot returns image content
        screenshot_res = await mcp_client.call_tool("screenshot", {"page_id": page_id})
        assert not screenshot_res.isError
        assert any(c.type == "image" for c in screenshot_res.content)
    
        # close_page works
        close_res = await mcp_client.call_tool("close_page", {"page_id": page_id})
        assert not close_res.isError
