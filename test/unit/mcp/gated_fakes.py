"""Fake session read / write methods that run the gate the tool hands them.

The real session runs the gate against the tab's live page inside the same
driver hold as the read or write. These fakes do the same against a canned
``url`` (``None`` stands for a tab that is gone): the gates run for real, and
``.performed`` records the call only if they passed. So a test asserts
``performed == []`` for a refusal — the method itself is always awaited now.
"""
from unittest.mock import AsyncMock

from browden.mcp.validator import ValidationError, tab_gone_envelope


def gated_write(*, url, elements, result):
    """A fake ``click`` / ``insert_text`` / ``press_key``: gates 1 and 2+ on ``elements``."""
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


def gated_read(*, url, result):
    """A fake DOM read / ``screenshot``: gates the tab's live ``url``, then returns ``result``."""
    async def read(*args, id, gate, **kwargs):
        if url is None:
            return tab_gone_envelope(id)
        gate.check_page(url)
        method.performed.append((args, kwargs))
        return result

    method = AsyncMock(side_effect=read)
    method.performed = []
    return method


def gated_list(*, tabs):
    """A fake ``list_tabs``: keeps the ``tabs`` the gate admits; ``.closed`` lists the ids it refused."""
    async def list_tabs(*, gate):
        kept = []
        for tab in tabs:
            try:
                gate.check_page(tab.get("url") or "")
            except ValidationError:
                method.closed.append(tab["id"])
                continue
            kept.append(tab)
        return kept

    method = AsyncMock(side_effect=list_tabs)
    method.closed = []
    return method
