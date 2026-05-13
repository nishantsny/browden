"""Serialize a BeautifulSoup element into a plain JSON-able dict (a "node").

Pure: no Selenium, no I/O. The caps below keep tool responses small. A single
Amazon order card is ~355 chars of text but ~167 KB of HTML, and a few
attributes (an ad iframe's ``name``) hold 15 KB of JSON — so raw HTML and raw
attribute values are never echoed by default; we truncate, report the true
length, and let the caller opt in to more (``include_html`` / ``max_html_bytes``).
"""

ATTR_CAP = 256          # max chars per attribute value
TEXT_CAP = 2000         # max chars of collapsed text
DEFAULT_MAX_HTML_BYTES = 4096


def _attr_value(value) -> str:
    """bs4 returns multi-valued attrs (class, rel, ...) as lists; flatten to a string."""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value)


def _classes(tag) -> list[str]:
    raw = tag.get("class")
    if not raw:
        return []
    if isinstance(raw, str):
        return raw.split()
    return list(raw)


def element_to_node(tag, *, include_html: bool = False,
                    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES) -> dict:
    """Return the JSON node for one bs4 ``Tag``. See module docstring for the caps."""
    el_id = tag.get("id")
    if isinstance(el_id, (list, tuple)):
        el_id = " ".join(str(v) for v in el_id)
    if not el_id:
        el_id = None

    attributes: dict[str, str] = {}
    attributes_truncated: list[str] = []
    for key, value in tag.attrs.items():
        if key in ("id", "class"):
            continue  # surfaced as dedicated fields
        s = _attr_value(value)
        if len(s) > ATTR_CAP:
            s = s[:ATTR_CAP]
            attributes_truncated.append(key)
        attributes[key] = s

    full_text = " ".join(tag.get_text(separator=" ", strip=True).split())
    text_length = len(full_text)
    text = full_text[:TEXT_CAP]
    text_truncated = text_length > TEXT_CAP

    html_str = str(tag)

    node = {
        "tag": tag.name,
        "id": el_id,
        "classes": _classes(tag),
        "attributes": attributes,
        "text": text,
        "text_length": text_length,
        "text_truncated": text_truncated,
        "html_length": len(html_str),
        "child_count": len(tag.find_all(recursive=False)),
    }
    if attributes_truncated:
        node["attributes_truncated"] = attributes_truncated
    if include_html:
        encoded = html_str.encode("utf-8")
        if len(encoded) > max_html_bytes:
            node["html"] = encoded[:max_html_bytes].decode("utf-8", "ignore")
            node["html_truncated"] = True
        else:
            node["html"] = html_str
            node["html_truncated"] = False
    return node
