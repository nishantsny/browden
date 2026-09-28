"""A pass-through ``WriteGate`` for e2e tests of the write *primitives*.

These tests drive ``BrowserSessionManager.click`` / ``insert_text`` /
``press_key`` against real Chrome to pin what the backend itself does (fire the
onclick, refuse a character key, refuse an ambiguous live match). The policy
gates are not under test here — they are unit-tested with real rules — so the
session is handed a gate that admits everything.
"""
from browden.mcp.validator import WriteGate

OPEN_GATE = WriteGate(check_page=lambda url: None,
                      check_element=lambda url, css_selector, found: None)
