import pytest

from browser_guard.dependencies.bs4 import BeautifulSoup
from browser_guard.dom import query

HTML = """
<div id="a" class="card hot"></div>
<div id="b" class="card"></div>
<div id="c" class="card hot extra"></div>
<p id="d" class="note"></p>
"""


def soup():
    return BeautifulSoup(HTML, "html.parser")


def test_by_id_returns_one_or_none():
    assert query.by_id(soup(), "b")["id"] == "b"
    assert query.by_id(soup(), "missing") is None


def test_by_class_requires_all_named_classes():
    page, _limit, _offset, total, _next = query.by_class(soup(), "card hot")
    assert total == 2
    assert sorted(t["id"] for t in page) == ["a", "c"]


def test_by_class_single_class_matches_all():
    _page, _limit, _offset, total, _next = query.by_class(soup(), "card")
    assert total == 3


def test_css_one_and_all():
    assert query.css_one(soup(), "p.note")["id"] == "d"
    assert query.css_one(soup(), "p.missing") is None
    _page, _l, _o, total, _n = query.css_all(soup(), "div.card")
    assert total == 3


def test_invalid_css_raises_invalid_selector():
    with pytest.raises(query.InvalidSelector):
        query.css_one(soup(), "div::::bogus")
    with pytest.raises(query.InvalidSelector):
        query.css_all(soup(), "??")


def test_pagination_basic_slice_and_next_offset():
    matches = list(range(25))
    page, eff_limit, eff_offset, total, next_offset = query.paginate(matches, limit=10, offset=0)
    assert page == list(range(10))
    assert (eff_limit, eff_offset, total, next_offset) == (10, 0, 25, 10)


def test_pagination_limit_clamped_to_max():
    page, eff_limit, _o, _t, _n = query.paginate(list(range(100)), limit=999, offset=0)
    assert eff_limit == query.LIMIT_MAX
    assert len(page) == query.LIMIT_MAX


def test_pagination_offset_equal_to_total_is_empty_not_error():
    page, _l, eff_offset, total, next_offset = query.paginate(list(range(5)), limit=10, offset=5)
    assert page == []
    assert eff_offset == 5
    assert total == 5
    assert next_offset is None


def test_pagination_offset_past_total():
    page, _l, _o, _t, next_offset = query.paginate(list(range(5)), limit=10, offset=99)
    assert page == []
    assert next_offset is None


def test_pagination_next_offset_null_exactly_when_exhausted():
    # 12 items, page of 5 starting at 7 -> returns last 5, exhausted
    _page, _l, _o, _t, next_offset = query.paginate(list(range(12)), limit=5, offset=7)
    assert next_offset is None
    # ...but starting at 6 leaves item 11 -> not exhausted
    _page, _l, _o, _t, next_offset = query.paginate(list(range(12)), limit=5, offset=6)
    assert next_offset == 11
