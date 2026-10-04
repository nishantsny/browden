"""The session's frame methods: locking, and what they leave behind on failure.

Policy (which frames may be entered) is pinned in test/unit/mcp/test_server_frames.py;
these tests pin the session's own contract around the backend.
"""
import pytest

from test_session import FakeBackend, make_session

TAB = "ns-h1"


class FrameFakeBackend(FakeBackend):
    """``FakeBackend`` plus a one-level frame model: ``in_frame`` is the focus."""

    def __init__(self):
        super().__init__()
        self.in_frame = False

    def get_frame_src(self, css_selector):
        self.calls.append(("get_frame_src", css_selector))
        return {"src": "https://example.test/child"}

    def enter_frame(self, css_selector):
        self.calls.append(("enter_frame", css_selector))
        self.in_frame = True
        return {"frame_url": "https://example.test/child", "top_url": "https://example.test/h1"}

    def switch_to_parent_frame(self):
        self.calls.append("switch_to_parent_frame")
        self.in_frame = False
        return {"frame_url": "https://example.test/h1", "top_url": "https://example.test/h1"}

    def switch_to_default_content(self):
        self.calls.append("switch_to_default_content")
        self.in_frame = False
        return {"frame_url": "https://example.test/h1", "top_url": "https://example.test/h1"}


@pytest.mark.asyncio
async def test_frame_methods_never_sweep_idle_tabs():
    # sweep_idle closes tabs, which switches the driver's window. It must only run
    # under the driver lock (the reaper's _sweep_idle_locked); a tool that swept
    # outside it could move another request's driver mid-read or mid-click.
    s = make_session(FrameFakeBackend())
    swept = []
    s.sweep_idle = lambda *a, **k: swept.append(a)

    await s.frame_src("#child", id=TAB)
    await s.enter_frame("#child", id=TAB)
    await s.switch_to_parent_frame(id=TAB)
    await s.switch_to_default_content(id=TAB)

    assert swept == []
