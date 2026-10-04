"""Tool-level tests for the frame focus tools: the real frame gates, real rules.

The tools build a ``FrameGate`` from the tab's profile rules and hand it to the
session, which runs it inside the one driver hold that moves the focus (pinned
in test/unit/web_navigator/test_session_frames.py). Here the session is a small
stand-in that runs the gate exactly as the real one does, against scripted
``frame_url`` / ``top_url`` pairs, so every verdict below comes from the real
gates:

* ``switch_to_frame`` gates the focused page, the iframe's declared src before
  switching, and the landed ``document.URL`` (read-allowed + same-origin) after;
* ``switch_to_parent_frame`` / ``switch_to_default_content`` RE-gate the landed
  ancestor/top on every call — another process may have navigated it to an
  untrusted page while we were deeper in the tree.
"""
from unittest.mock import patch

import pytest

from browden.configs.loader import RuntimeConfigurationRefresher
from browden.mcp.validator import BrowdenRuntimeConfiguration, FrameGate, ValidationError

# Only app.example.com is readable...
_READ_APP = BrowdenRuntimeConfiguration({"read": {"website_overrides": {"app.example.com": [".*"]}}})
# ...vs the whole https web readable (to isolate the same-origin check from the
# read-allowed check).
_READ_OPEN = BrowdenRuntimeConfiguration({"read": {"website_overrides": {"*": [".*"]}}})

TOP = "https://app.example.com/page"
WIDGET = "https://app.example.com/widget"


class GateRunningSession:
    """Runs the handed ``FrameGate`` the way ``BrowserSessionManager`` does.

    ``page`` is the focused document, ``src`` the iframe's declared src (None for
    srcdoc), ``landed`` the document a move lands on. ``moves`` records what the
    focus did: ``"entered"``, ``"up"``, ``"top"``, ``"retreated"``.
    """

    profile_dir = "/fake/profile"  # no `profiles:` block in these rules → the global ones

    def __init__(self, page=TOP, src=None, landed=WIDGET, top=TOP):
        self.page, self.src, self.landed, self.top = page, src, landed, top
        self.moves: list[str] = []
        self.gates: list[FrameGate] = []

    async def enter_frame(self, css_selector, *, id, gate):
        self.gates.append(gate)
        gate.check_page(self.page)
        if self.src:
            gate.check_src(self.src)
        gate.check_landed(self.top, self.landed)  # the backend rolls back on a raise
        self.moves.append("entered")
        return {"frame_url": self.landed, "top_url": self.top, "id": id}

    async def switch_to_parent_frame(self, *, id, gate):
        self.gates.append(gate)
        self.moves.append("up")
        try:
            gate.check_landed(self.top, self.landed)
        except ValidationError:
            self.moves.append("retreated")
            raise
        return {"frame_url": self.landed, "top_url": self.top, "id": id}

    async def switch_to_default_content(self, *, id, gate):
        self.gates.append(gate)
        self.moves.append("top")
        gate.check_page(self.top)  # back at the top: only the read check applies
        return {"frame_url": self.top, "top_url": self.top, "id": id}


def _server():
    import browden.mcp.server as server
    __import__("importlib").reload(server)
    return server


async def _call(tool, rules, session, *args):
    server = _server()
    with patch.object(server._store, "route", return_value=session), \
         patch.object(server, "_refresher", RuntimeConfigurationRefresher.static(rules)):
        return await getattr(server, tool)(*args)


# -- switch_to_frame (entry gate: page, src before, landed after) -------------

@pytest.mark.asyncio
async def test_switch_to_frame_same_origin_allowed():
    session = GateRunningSession(src=WIDGET)
    result = await _call("switch_to_frame", _READ_OPEN, session, "#child", "t")
    assert result["frame_url"] == WIDGET
    assert session.moves == ["entered"]
    assert len(session.gates) == 1 and isinstance(session.gates[0], FrameGate)


@pytest.mark.asyncio
async def test_switch_to_frame_refuses_when_the_focused_page_is_not_readable():
    session = GateRunningSession(page="https://evil.com/top")
    with pytest.raises(ValidationError, match="not on the read allowlist"):
        await _call("switch_to_frame", _READ_APP, session, "#child", "t")
    assert session.moves == []


@pytest.mark.asyncio
async def test_switch_to_frame_refuses_already_untrusted_src_before_switching():
    # The iframe is ALREADY pointed at an untrusted site: its declared src fails
    # the pre-switch gate, so we never enter it.
    session = GateRunningSession(src="https://evil.com/ad")
    with pytest.raises(ValidationError, match="not on allowlist"):
        await _call("switch_to_frame", _READ_APP, session, "#child", "t")
    assert session.moves == []  # refused BEFORE switching


@pytest.mark.asyncio
async def test_switch_to_frame_refuses_cross_origin_landed_document():
    # src looked innocent / redirected, but the landed document is a different
    # origin. Whole web readable, so ONLY the same-origin check can refuse.
    session = GateRunningSession(src="https://app.example.com/redirector",
                                 landed="https://ads.other.com/frame")
    with pytest.raises(ValidationError, match="cross-origin"):
        await _call("switch_to_frame", _READ_OPEN, session, "#child", "t")
    assert session.moves == []


# -- switch_to_parent_frame / switch_to_default_content (RE-gate on ascent) ----

@pytest.mark.asyncio
async def test_parent_frame_same_origin_allowed():
    session = GateRunningSession(landed="https://app.example.com/parent")
    result = await _call("switch_to_parent_frame", _READ_OPEN, session, "t")
    assert result["frame_url"] == "https://app.example.com/parent"
    assert session.moves == ["up"]


@pytest.mark.asyncio
async def test_parent_frame_refuses_when_ancestor_moved_cross_origin():
    # While we were deeper in the tree, another process navigated the PARENT frame
    # to a cross-origin page. Ascending re-verifies the landed document, refuses,
    # and retreats to the top.
    session = GateRunningSession(landed="https://evil.com/hijacked")
    with pytest.raises(ValidationError, match="cross-origin"):
        await _call("switch_to_parent_frame", _READ_OPEN, session, "t")
    assert session.moves == ["up", "retreated"]


@pytest.mark.asyncio
async def test_default_content_refuses_when_top_moved_to_untrusted():
    # Another process moved the TOP page itself to an untrusted URL since we
    # descended. Returning to default content re-checks it and refuses (there is
    # nowhere safer to retreat — the read tools also refuse to read it).
    session = GateRunningSession(top="https://evil.com/landing")
    with pytest.raises(ValidationError, match="not on the read allowlist"):
        await _call("switch_to_default_content", _READ_APP, session, "t")


@pytest.mark.asyncio
async def test_default_content_allowed_when_top_still_trusted():
    session = GateRunningSession()
    result = await _call("switch_to_default_content", _READ_APP, session, "t")
    assert result["top_url"] == TOP


@pytest.mark.asyncio
async def test_default_content_on_an_about_blank_tab_is_allowed():
    # A fresh tab, or one bounced to about:blank: about:blank has no origin, but
    # returning to the top page needs only the read check, which always admits it.
    session = GateRunningSession(top="about:blank")
    result = await _call("switch_to_default_content", _READ_APP, session, "t")
    assert result["top_url"] == "about:blank"
