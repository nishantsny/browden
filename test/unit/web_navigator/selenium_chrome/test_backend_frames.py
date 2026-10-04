"""The Selenium backend's frame primitives, against a fake WebDriver.

Only the driver is fake (``FakeDriver``: one window, a tree of documents keyed by
URL, and the scripts the backend runs). Every method of the backend runs for
real — the frame lookup, ``_vet``, the rollback, the replay — so a helper that is
renamed or removed fails here, not in Chrome.
"""
import pytest

from safe_agent_browser.dependencies.selenium import JavascriptException
from safe_agent_browser.web_navigator.interface import FrameFocusError, InvalidSelectorError
from safe_agent_browser.web_navigator.selenium_chrome.backend import SeleniumChromeBackend

TOP = "https://app.example.com/page"
CHILD = "https://app.example.com/widget"
MID = "https://app.example.com/mid"
FOREIGN = "https://ads.other.example/frame"
ELSEWHERE = "https://app.example.com/elsewhere"


class Refused(Exception):
    pass


def _ok(*_):
    return None


def _refuse(*_):
    raise Refused("no")


class FakeEl:
    """An element; for a frame, ``doc`` is the URL of the document inside it."""

    def __init__(self, doc=None, tag="iframe", src=None, displayed=True):
        self.doc, self.tag, self.src, self.displayed = doc, tag, src, displayed

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return True


class _SwitchTo:
    def __init__(self, drv):
        self._drv = drv

    def window(self, handle):
        self._drv.focused = handle
        self._drv.stack = []  # a window focus lands on its top document

    def frame(self, el):
        self._drv.stack.append(el.doc)

    def parent_frame(self):
        if self._drv.stack:
            self._drv.stack.pop()

    def default_content(self):
        self._drv.stack = []


class FakeDriver:
    """A WebDriver over ``docs``: document URL -> {selector: [elements]}.

    ``stack`` is the frame focus (document URLs, outermost first). ``url_script_error``
    makes the document-URL script raise, as it can in a page that blocks script.
    """

    def __init__(self, docs, top=TOP):
        self.docs = docs
        self.top = top
        self.stack: list[str] = []
        self.window_handles = ["h1"]
        self.current_window_handle = "h1"
        self.title = "t"
        self.switch_to = _SwitchTo(self)
        self.focused = "h1"
        self.url_script_error = None

    @property
    def current_url(self):
        return self.top

    def _doc(self):
        # A #fragment doesn't change the document the page holds.
        return self.stack[-1] if self.stack else self.top.split("#", 1)[0]

    def find_elements(self, by, selector):
        return list(self.docs.get(self._doc(), {}).get(selector, []))

    def execute_script(self, script, *args):
        if "querySelectorAll" in script:  # the frame-target script
            selector = args[0]
            if selector.startswith("!!"):
                raise JavascriptException(msg=f"'{selector}' is not a valid selector")
            els = self.find_elements(None, selector)
            first = els[0] if els else None
            return [len(els), first and first.tag, first and first.src, first]
        if "effectiveUrl" in script:      # the document-URL script
            if self.url_script_error is not None:
                raise self.url_script_error
            return self._doc()
        raise AssertionError(f"unexpected script: {script[:60]!r}")

    def get(self, url):
        self.top, self.stack = url, []

    def refresh(self):
        self.stack = []

    def close(self):
        self.window_handles = [h for h in self.window_handles if h != self.focused]


def _backend(docs=None, top=TOP):
    drv = FakeDriver(docs if docs is not None else {TOP: {"#child": [FakeEl(CHILD, src=CHILD)]}},
                     top=top)
    backend = SeleniumChromeBackend(profile_dir="/tmp/bg-unit-profile")
    backend._driver = drv  # attach the fake driver; _drv()'s real health check passes
    backend.select_tab("h1")
    return backend, drv


# -- entering ---------------------------------------------------------------------

def test_enter_frame_records_the_frame_only_after_both_checks_pass():
    backend, drv = _backend()
    seen = []
    result = backend.enter_frame("#child", lambda src: seen.append(("src", src)),
                                 lambda top, frame: seen.append(("landed", top, frame)))
    assert seen == [("src", CHILD), ("landed", TOP, CHILD)]
    assert result == {"frame_url": CHILD, "top_url": TOP}
    assert drv.stack == [CHILD] and backend.in_frame() is True


def test_a_refused_src_never_switches():
    backend, drv = _backend()
    with pytest.raises(Refused):
        backend.enter_frame("#child", _refuse, _ok)
    assert drv.stack == [] and backend.in_frame() is False


def test_a_src_less_frame_skips_the_src_check_and_is_judged_on_landing():
    backend, drv = _backend({TOP: {"#child": [FakeEl(TOP, src=None)]}})  # srcdoc: judged by parent
    src_checks, landed = [], []
    backend.enter_frame("#child", src_checks.append, lambda *a: landed.append(a))
    assert src_checks == [] and landed == [(TOP, TOP)]


def test_a_refused_landing_rolls_back_and_records_nothing():
    backend, drv = _backend({TOP: {"#child": [FakeEl(FOREIGN, src=CHILD)]}})
    with pytest.raises(Refused):
        backend.enter_frame("#child", _ok, _refuse)
    assert drv.stack == [] and backend.in_frame() is False


def test_a_url_script_failure_after_switching_rolls_back_too():
    backend, drv = _backend()
    drv.url_script_error = JavascriptException(msg="script blocked")
    landed = []
    with pytest.raises(JavascriptException):
        backend.enter_frame("#child", _ok, lambda *a: landed.append(a))
    assert landed == []                                   # never judged...
    assert drv.stack == [] and backend.in_frame() is False  # ...and never left focused


def test_a_rollback_one_level_down_returns_to_the_recorded_parent_frame():
    backend, drv = _backend({TOP: {"#mid": [FakeEl(MID, src=MID)]},
                             MID: {"#child": [FakeEl(FOREIGN, src=CHILD)]}})
    backend.enter_frame("#mid", _ok, _ok)
    with pytest.raises(Refused):
        backend.enter_frame("#child", _ok, _refuse)
    assert drv.stack == [MID]  # back in the parent frame, which stays recorded
    assert backend.document_url() == MID


# -- which element: exactly one live iframe, vetted like a write target ---------

@pytest.mark.parametrize("els,match", [
    ([], "matched 0 elements"),
    ([FakeEl(CHILD, src=CHILD), FakeEl(CHILD, src=CHILD)], "matched 2 elements"),
    ([FakeEl(CHILD, src=CHILD, displayed=False)], "not visible"),
    ([FakeEl(tag="div")], "is a <div>"),
])
def test_enter_frame_needs_exactly_one_visible_iframe(els, match):
    backend, drv = _backend({TOP: {"#child": els}})
    with pytest.raises(ValueError, match=match):
        backend.enter_frame("#child", _ok, _ok)
    assert drv.stack == []


def test_enter_frame_reports_an_invalid_selector_like_a_write_does():
    backend, _ = _backend()
    with pytest.raises(InvalidSelectorError):
        backend.enter_frame("!!bad", _ok, _ok)


# -- the URL the gates judge --------------------------------------------------------

def test_document_url_inside_a_frame_refuses_rather_than_falling_back_to_the_top():
    backend, drv = _backend()
    backend.enter_frame("#child", _ok, _ok)
    drv.url_script_error = JavascriptException(msg="script blocked")
    with pytest.raises(FrameFocusError):
        backend.document_url()


def test_document_url_at_the_top_still_falls_back_to_current_url():
    # e.g. a chrome:// page, where script can't run: the read policy special-cases it.
    backend, drv = _backend()
    drv.url_script_error = JavascriptException(msg="script blocked")
    assert backend.document_url() == TOP


def test_retreat_to_top_forgets_the_path():
    backend, drv = _backend()
    backend.enter_frame("#child", _ok, _ok)
    backend.retreat_to_top()
    assert drv.stack == [] and backend.in_frame() is False


# -- replay: the recorded frame is re-entered only while it is still the one admitted

def test_the_frame_is_re_entered_on_every_refocus():
    backend, drv = _backend()
    backend.enter_frame("#child", _ok, _ok)
    for _ in range(3):
        backend.select_tab("h1")  # a window focus drops to the top; the replay re-enters
        assert drv.stack == [CHILD] and backend.document_url() == CHILD


def test_a_fragment_change_on_the_top_page_keeps_the_frame():
    backend, drv = _backend()
    backend.enter_frame("#child", _ok, _ok)
    drv.top = TOP + "#section-2"
    backend.select_tab("h1")
    assert drv.stack == [CHILD]


def _lost(backend):
    """The tab is at its top, and the next gated op reports the lost frame — once."""
    assert backend.in_frame() is False
    with pytest.raises(FrameFocusError, match="gone or has changed"):
        backend.document_url()
    assert backend.document_url() == backend._driver.current_url  # reported once; now at the top


def test_a_removed_frame_errors_once_instead_of_reading_the_top():
    backend, drv = _backend()
    backend.enter_frame("#child", _ok, _ok)
    drv.docs[TOP]["#child"] = []  # the page removed it
    backend.select_tab("h1")
    _lost(backend)


def test_a_top_page_that_moved_is_not_re_entered_even_with_a_matching_frame():
    # The tab was inside #child on TOP. TOP's JS navigates the tab to a page that
    # also has a #child (pointing anywhere): the replay must not enter it.
    backend, drv = _backend({TOP: {"#child": [FakeEl(CHILD, src=CHILD)]},
                             ELSEWHERE: {"#child": [FakeEl(CHILD, src=CHILD)]}})
    backend.enter_frame("#child", _ok, _ok)
    drv.top = ELSEWHERE
    backend.select_tab("h1")
    assert drv.stack == []
    _lost(backend)


def test_a_frame_swapped_for_another_origins_is_not_re_entered():
    # Same top page, same selector — but the page replaced the iframe with one
    # holding another origin's document. "Same-origin" holds on every op, not
    # just on entry.
    backend, drv = _backend()
    backend.enter_frame("#child", _ok, _ok)
    drv.docs[TOP]["#child"] = [FakeEl(FOREIGN, src=FOREIGN)]
    backend.select_tab("h1")
    assert drv.stack == []
    _lost(backend)


def test_a_lost_frame_refuses_an_ascent():
    backend, drv = _backend()
    backend.enter_frame("#child", _ok, _ok)
    drv.docs[TOP]["#child"] = []
    backend.select_tab("h1")
    with pytest.raises(FrameFocusError):
        backend.switch_to_parent_frame()


@pytest.mark.parametrize("leave", ["switch_to_default_content", "navigate", "reload"])
def test_leaving_on_purpose_clears_a_lost_frame_silently(leave):
    backend, drv = _backend()
    backend.enter_frame("#child", _ok, _ok)
    drv.docs[TOP]["#child"] = []
    backend.select_tab("h1")
    getattr(backend, leave)(*([TOP] if leave == "navigate" else []))
    assert backend.document_url() == TOP  # no FrameFocusError: the agent left on purpose


def test_closing_a_tab_forgets_its_frames():
    backend, drv = _backend()
    drv.window_handles = ["h1", "h2"]
    backend.enter_frame("#child", _ok, _ok)
    backend.close_tab("h1")
    backend.select_tab("h2")
    assert backend.in_frame() is False


# -- ascents: the landed URL when script can't run --------------------------------

@pytest.mark.parametrize("ascend", ["switch_to_default_content", "switch_to_parent_frame"])
def test_an_ascent_to_the_top_falls_back_to_current_url_when_script_cant_run(ascend):
    # e.g. a chrome:// top page, which runs no script: report the top URL (the read
    # policy special-cases it) rather than raise the driver's error.
    backend, drv = _backend()
    backend.enter_frame("#child", _ok, _ok)
    drv.url_script_error = JavascriptException(msg="script blocked")
    result = getattr(backend, ascend)()
    assert result == {"frame_url": TOP, "top_url": TOP}


def test_an_ascent_that_stays_inside_a_frame_refuses_when_script_cant_run():
    backend, drv = _backend({TOP: {"#mid": [FakeEl(MID, src=MID)]},
                             MID: {"#child": [FakeEl(CHILD, src=CHILD)]}})
    backend.enter_frame("#mid", _ok, _ok)
    backend.enter_frame("#child", _ok, _ok)
    drv.url_script_error = JavascriptException(msg="script blocked")
    with pytest.raises(FrameFocusError):
        backend.switch_to_parent_frame()  # lands in #mid, whose URL can't be read
