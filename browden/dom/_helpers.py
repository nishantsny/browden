"""Internal helpers shared between ``query`` and ``serialize`` — kept out of
``__init__.py`` to avoid a circular import (the package's ``__init__`` re-exports
``query`` / ``serialize``, which would deadlock if they imported from it).
"""


def tag_class_list(tag) -> list[str]:
    """Return a tag's class names as a list, normalizing bs4's ``str | list`` representation.

    bs4 returns multi-valued attributes (``class``, ``rel``, …) as a list for parsed
    HTML, but as a plain string when an element is constructed directly. Both
    serialization and query code need to read classes, so the quirk lives here.
    """
    raw = tag.get("class") if hasattr(tag, "get") else None
    if not raw:
        return []
    if isinstance(raw, str):
        return raw.split()
    return list(raw)
