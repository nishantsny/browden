"""Single owner of the public page-id wire format: ``<namespace>-<handle>``.

The MCP server mints a namespace per profile (a hex digest of the profile
path, so it can never contain the separator) and stamps it onto the backend;
incoming page_ids are routed back to their session by splitting on the same
separator. Both sides go through these helpers so the format is defined in
exactly one place.
"""

SEPARATOR = "-"


def format_page_id(namespace: str, handle: str) -> str:
    return f"{namespace}{SEPARATOR}{handle}"


def split_page_id(page_id: str) -> tuple[str, str]:
    """Split a public id into ``(namespace, handle)``.

    The namespace never contains the separator, so the first occurrence is
    the boundary (the handle may contain more — Selenium's do). An
    un-namespaced id yields ``("", page_id)``.
    """
    namespace, sep, handle = page_id.partition(SEPARATOR)
    if not sep:
        return "", page_id
    return namespace, handle
