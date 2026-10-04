"""The DOM-read caps must be discoverable from the MCP surface, not only the source.

A caller sees a tool's description and its JSON schema, and nothing else. The caps
in ``dom/serialize.py`` are sensible, but when they reached no further than the
module that implements them, a truncated ``text`` read as "this tool clamps
output" and the way to get the whole node (``include_html`` + a large
``max_html_bytes``) was invisible — see issue #148.

These tests pin the documentation to the constants it describes, so a future
change to a cap goes red here instead of quietly making the docs wrong.
"""
import re
from pathlib import Path

from browden.dom import query, serialize

READ_TOOLS = ("get_element_by_id", "get_elements_by_class_name",
              "query_selector", "query_selector_all")
LIST_TOOLS = ("get_elements_by_class_name", "query_selector_all")

DOCS = Path(__file__).resolve().parents[3] / "docs" / "dom-reads.md"
README = Path(__file__).resolve().parents[3] / "README.md"


async def _schemas():
    import browden.mcp.server as server
    return {t.name: t.inputSchema for t in await server.mcp.list_tools()}


async def test_every_read_tool_describes_include_html_and_max_html_bytes():
    """The two params a caller gets wrong are bare typed fields without this."""
    schemas = await _schemas()
    for name in READ_TOOLS:
        props = schemas[name]["properties"]
        include_html = props["include_html"]["description"].lower()
        max_html_bytes = props["max_html_bytes"]["description"].lower()
        # include_html is the only route past the text cap — say so where it's read.
        assert "html" in include_html
        assert str(serialize.TEXT_CAP) in include_html
        # ...and max_html_bytes is inert without it, which is the trap.
        assert "include_html=true" in max_html_bytes
        assert "html_truncated" in max_html_bytes


async def test_list_tools_say_pagination_is_over_elements():
    """`limit`/`offset` page over matches, not over the text inside one match."""
    schemas = await _schemas()
    for name in LIST_TOOLS:
        for param in ("limit", "offset"):
            description = schemas[name]["properties"][param]["description"].lower()
            assert "element" in description
            assert "within a single element" in description


async def test_every_read_tool_docstring_states_the_text_cap():
    import browden.mcp.server as server
    for name in READ_TOOLS:
        doc = getattr(server, name).__doc__ or ""
        assert str(serialize.TEXT_CAP) in doc
        assert "text_truncated" in doc
        assert "include_html" in doc


def test_the_reference_page_documents_the_real_caps():
    """docs/dom-reads.md quotes numbers; they have to be the ones in the code."""
    text = DOCS.read_text()
    assert f"**{serialize.TEXT_CAP} chars**" in text
    assert f"{serialize.ATTR_CAP} chars" in text
    assert f"**{serialize.DEFAULT_MAX_HTML_BYTES} bytes**" in text
    assert f"clamped to {query.LIMIT_MAX}" in text
    assert f"default {query.LIMIT_DEFAULT}" in text


def test_the_reference_page_documents_every_field_of_a_node():
    """A field the serializer emits but the page never names is undiscoverable."""
    emitted = {
        "tag", "id", "classes", "attributes", "text", "text_length",
        "text_truncated", "html_length", "child_count", "attributes_truncated",
        "labelledby_text", "field_label", "html", "html_truncated",
    }
    documented = set(re.findall(r"^\| `(\w+)` \|", DOCS.read_text(), re.MULTILINE))
    assert emitted <= documented


def test_the_readme_points_at_the_reference_page():
    assert "docs/dom-reads.md" in README.read_text()
