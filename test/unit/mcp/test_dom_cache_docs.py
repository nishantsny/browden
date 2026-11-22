"""The DOM cache contract must be readable from the MCP surface.

The read tools answer from a per-tab snapshot, and what drops that snapshot
decides whether a read shows the page as it is now. A caller sees only a tool's
description, so the contract has to be stated there: on the write tools (the
read after a write is fresh, and the write is not what rebuilds the snapshot), on
the read tools (they answer from a snapshot at all) and on the reference page.

The behaviour itself is pinned in test_gated_page.py and
test/e2e/test_dom_cache_invalidation.py. These tests keep the descriptions from
losing it.
"""
from pathlib import Path

from browden.web_navigator.soup_cache import TTL_SECONDS

READ_TOOLS = ("get_element_by_id", "get_elements_by_class_name",
              "query_selector", "query_selector_all")
WRITE_TOOLS = ("click", "insert_text", "press_key", "upload_file")
FRAME_TOOLS = ("switch_to_frame", "switch_to_parent_frame", "switch_to_default_content")

DOCS = Path(__file__).resolve().parents[3] / "docs" / "dom-reads.md"
README = Path(__file__).resolve().parents[3] / "README.md"


async def _descriptions():
    """Each tool's description as a caller receives it, on one line."""
    import browden.mcp.server as server
    return {t.name: " ".join((t.description or "").split()) for t in await server.mcp.list_tools()}


def _cache_section():
    text = DOCS.read_text()
    start = text.index("## The DOM cache")
    return text[start:text.index("\n## ", start + 1)]


async def test_every_write_tool_says_the_next_read_is_fresh():
    descriptions = await _descriptions()
    for name in WRITE_TOOLS:
        description = descriptions[name]
        assert "snapshot is dropped" in description, name
        assert "next read re-fetches the live page" in description, name
        # ...and that the write does not rebuild it, so a page that reacts late
        # needs invalidate_dom_cache before it is read again.
        assert "That read rebuilds the snapshot, not this call" in description, name
        assert "invalidate_dom_cache" in description, name
        assert "docs/dom-reads.md" in description, name


async def test_every_write_tool_says_its_selector_is_matched_live():
    descriptions = await _descriptions()
    for name in WRITE_TOOLS:
        assert "matched against the live page, not the cached snapshot" in descriptions[name], name


async def test_every_read_tool_says_it_answers_from_a_snapshot_and_what_retakes_it():
    descriptions = await _descriptions()
    for name in READ_TOOLS:
        description = descriptions[name]
        assert "cached DOM snapshot" in description, name
        for dropper in ("navigate", *WRITE_TOOLS, "frame switch", "invalidate_dom_cache"):
            assert dropper in description, (name, dropper)


async def test_invalidate_dom_cache_says_browdens_own_actions_do_not_need_it():
    description = (await _descriptions())["invalidate_dom_cache"]
    assert "Not needed after browden's own actions" in description
    for name in ("navigate", *WRITE_TOOLS):
        assert name in description, name


async def test_force_reload_tab_says_when_its_snapshot_is_taken():
    description = (await _descriptions())["force_reload_tab"]
    assert "The snapshot is retaken in this call, as soon as the page has loaded" in description
    assert "invalidate_dom_cache" in description


def test_the_reference_page_has_a_table_row_for_everything_that_drops_the_snapshot():
    # The row's first cell, not the section at large: the tool names also appear
    # in the prose under the table.
    rows = dict(line.strip("|").split("|", 1) for line in _cache_section().splitlines()
                if line.startswith("| `"))
    listed = " ".join(rows)
    for name in ("navigate", *WRITE_TOOLS, *FRAME_TOOLS, "invalidate_dom_cache", "force_reload_tab"):
        assert f"`{name}`" in listed, name
    write_row = next(effect for what, effect in rows.items() if "`click`" in what)
    assert "fails partway" in write_row
    assert "leaves the snapshot alone" in write_row


def test_the_reference_page_quotes_the_real_snapshot_lifetime():
    assert TTL_SECONDS % 3600 == 0
    hours = TTL_SECONDS // 3600
    lifetime = f"{hours} hour" if hours == 1 else f"{hours} hours"
    assert f"The snapshot is {lifetime} old" in _cache_section()
    assert f"expired ({lifetime})" in DOCS.read_text()


def test_the_readme_points_at_the_cache_section():
    assert "docs/dom-reads.md#the-dom-cache-which-page-a-read-sees" in README.read_text()
    assert "## The DOM cache: which page a read sees" in DOCS.read_text()
