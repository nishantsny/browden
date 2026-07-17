"""End-to-end: drive a real (headless) Chrome and capture a screenshot.

Self-contained — renders an inline ``data:`` document instead of hitting the
network, so the test is deterministic and needs no allowlisted host. Runs
headless via ``BROWDEN_HEADLESS`` (the conftest fixture sets it), so it
works on a CI runner or any machine without a display.
"""
import pytest

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# A trivially-rendered page: a full-viewport coloured box so there are real
# pixels to capture.
DATA_URL = (
    "data:text/html,"
    "<html><body style='margin:0'>"
    "<div style='width:100vw;height:100vh;background:#3366cc'>hello</div>"
    "</body></html>"
)


@pytest.fixture
def backend(new_backend, tmp_path):
    return new_backend(tmp_path / "profile")


def test_screenshot_returns_png_bytes(backend):
    backend.new_blank_tab()
    page = backend.navigate(DATA_URL)

    png = backend.screenshot(page.handle)

    assert isinstance(png, bytes)
    assert png.startswith(PNG_MAGIC)
    assert len(png) > 1000  # a real rendered viewport, not an empty stub


def test_screenshot_defaults_to_active_tab(backend):
    backend.new_blank_tab()
    backend.navigate(DATA_URL)

    png = backend.screenshot()  # no handle -> active tab

    assert png.startswith(PNG_MAGIC)
