"""A fake session write method (``click`` / ``insert_text`` / ``press_key``).

The real session runs the ``gate`` the tool hands it against the tab's live page
and only then acts, all in one driver hold. This fake does the same against a
canned ``url`` and ``elements``: the gates run for real, and ``.performed``
records the action only if they all passed. So a test asserts
``performed == []`` for a refusal — the method itself is always awaited now.
"""
from unittest.mock import AsyncMock

from browden.mcp.validator import tab_gone_envelope


def gated_write(*, url, elements, result):
    """``url=None`` stands for a tab that is gone."""
    found = {"total_count": len(elements), "elements": elements}

    async def write(css_selector, *args, id, gate):
        if url is None:
            return tab_gone_envelope(id)
        gate.check_page(url)
        gate.check_element(url, css_selector, found)
        method.performed.append((css_selector, *args))
        return {**result, "id": id}

    method = AsyncMock(side_effect=write)
    method.performed = []
    return method
