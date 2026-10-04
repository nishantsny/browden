"""Only ``GatedPage`` may read page content or act on a page.

The gates are only as good as the guarantee that nothing reaches page content
around them (docs/design/gate-atomicity.md). That guarantee is structural:
``gated_page.py`` is the one module that calls the backend's content methods
or fills the soup cache, so a new tool can't grow a check-then-read across two
driver holds without this test failing.

Every backend method is classified below as LIFECYCLE (tab bookkeeping, no page
content) or CONTENT. A method added to the backend interface without being
classified fails the build, so the default for anything new is "content".
"""
import ast
from pathlib import Path

from browden.web_navigator.interface import WebNavigatorBackend

ROOT = Path(__file__).resolve().parents[3] / "browden"
GATED_PAGE = ROOT / "mcp" / "session_management" / "gated_page.py"
# The backend's own definition and implementation are where these methods live.
EXEMPT = {ROOT / "web_navigator" / "interface.py", GATED_PAGE}
EXEMPT_DIRS = {ROOT / "web_navigator" / "selenium_chrome"}

LIFECYCLE = {"get_profile_dir", "is_running", "shutdown", "list_tabs", "list_handles",
             "new_blank_tab", "close_tab", "select_tab"}
CONTENT = {"page_snapshot", "target_snapshot", "document_url", "current_url", "screenshot",
           "navigate", "reload", "click_target", "insert_text_target", "press_key_target"}
# Content method names no other class in browden uses, so any call of them —
# whatever the receiver is called — is a backend call. (``navigate``,
# ``screenshot`` and ``reload`` are also session / GatedPage methods; they are
# caught by their receiver instead.)
BACKEND_ONLY_NAMES = CONTENT - {"navigate", "screenshot", "reload"}
BACKEND_RECEIVERS = {"backend", "_backend"}


def _receiver(node: ast.Attribute) -> str | None:
    v = node.value
    if isinstance(v, ast.Name):
        return v.id
    if isinstance(v, ast.Attribute):
        return v.attr
    return None


def violations(source: str, where: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        receiver = _receiver(node)
        if node.attr in BACKEND_ONLY_NAMES:
            found.append(f"{where}:{node.lineno}: .{node.attr}")
        elif receiver in BACKEND_RECEIVERS and node.attr not in LIFECYCLE:
            found.append(f"{where}:{node.lineno}: {receiver}.{node.attr}")
        elif receiver == "_cache" and node.attr == "put":
            found.append(f"{where}:{node.lineno}: _cache.put")
    return found


def _modules():
    for path in sorted(ROOT.rglob("*.py")):
        if path in EXEMPT or any(d in path.parents for d in EXEMPT_DIRS):
            continue
        yield path


def test_every_backend_method_is_classified():
    methods = set(WebNavigatorBackend.__abstractmethods__)
    assert LIFECYCLE.isdisjoint(CONTENT)
    unclassified = methods - LIFECYCLE - CONTENT
    assert not unclassified, (
        f"classify {sorted(unclassified)} as LIFECYCLE or CONTENT in {Path(__file__).name}")
    assert not (LIFECYCLE | CONTENT) - methods, "a classified method left the interface"


def test_only_gated_page_touches_page_content():
    found = [v for path in _modules()
             for v in violations(path.read_text(), str(path.relative_to(ROOT.parent)))]
    assert found == [], ("page content reached outside GatedPage — route it through "
                         "browden/mcp/session_management/gated_page.py:\n  " + "\n  ".join(found))


def test_gated_page_is_where_the_content_calls_are():
    # Guards the guard: if GatedPage stopped matching, the scan above would pass vacuously.
    assert violations(GATED_PAGE.read_text(), "gated_page.py")


def test_the_guard_catches_the_shapes_a_bypass_would_take():
    bypasses = [
        "self._backend.document_url()",                 # the separate-hold check
        "self._backend.page_snapshot()",
        "self._backend.screenshot()",
        "backend.navigate(url)",
        "b = self._backend\nb.target_snapshot('#x')",  # aliased receiver
        "self._cache.put(handle, url, soup)",
    ]
    for src in bypasses:
        assert violations(src, "x"), src
    for src in ["self._backend.select_tab(h)", "session.navigate(url, id=i, gate=g)",
                "page.screenshot(gate)", "self._backend.list_handles()"]:
        assert not violations(src, "x"), src
