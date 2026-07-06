# SPDX-FileCopyrightText: 2026 Nishant
# SPDX-License-Identifier: Apache-2.0

"""The four DOM-query primitives plus the pagination math the list ones share.

Pure: each takes a parsed ``BeautifulSoup`` tree and returns bs4 elements (or a
paginated slice of them). No Selenium, no serialization. Invalid CSS is raised as
``InvalidSelector`` for the caller to translate into a tool error.
"""
from ..dependencies.bs4 import SelectorSyntaxError
from ._helpers import tag_class_list

LIMIT_DEFAULT = 10
LIMIT_MAX = 50


class InvalidSelector(ValueError):
    """A CSS selector was syntactically invalid."""


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        return 1
    if limit > LIMIT_MAX:
        return LIMIT_MAX
    return limit


def paginate(matches, limit: int, offset: int):
    """Slice ``matches`` for one tab.

    Returns ``(tab, effective_limit, effective_offset, total_count, next_offset)``
    where ``next_offset`` is ``None`` exactly when pagination is exhausted
    (``offset + returned >= total_count``), including when ``offset >= total_count``.
    """
    total = len(matches)
    eff_limit = _clamp_limit(limit)
    eff_offset = max(0, offset)
    tab = matches[eff_offset:eff_offset + eff_limit]
    returned = len(tab)
    next_offset = None if eff_offset + returned >= total else eff_offset + returned
    return tab, eff_limit, eff_offset, total, next_offset


def by_id(soup, element_id: str):
    """Mirror ``document.getElementById`` — document-global, one element or ``None``."""
    return soup.find(id=element_id)


def by_class(soup, class_names: str, limit: int = LIMIT_DEFAULT, offset: int = 0):
    """Mirror ``document.getElementsByClassName`` — element must carry ALL named classes."""
    required = set(class_names.split())
    matches = soup.find_all(lambda t: required.issubset(tag_class_list(t)))
    return paginate(matches, limit, offset)


def css_one(soup, selector: str):
    """Mirror ``document.querySelector``. Raises ``InvalidSelector`` on bad CSS."""
    try:
        return soup.select_one(selector)
    except SelectorSyntaxError as e:
        raise InvalidSelector(str(e)) from e


def css_all(soup, selector: str, limit: int = LIMIT_DEFAULT, offset: int = 0):
    """Mirror ``document.querySelectorAll``. Raises ``InvalidSelector`` on bad CSS."""
    try:
        matches = soup.select(selector)
    except SelectorSyntaxError as e:
        raise InvalidSelector(str(e)) from e
    return paginate(matches, limit, offset)
