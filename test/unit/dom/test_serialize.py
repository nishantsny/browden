from browser_guard.dependencies.bs4 import BeautifulSoup
from browser_guard.dom.serialize import ATTR_CAP, TEXT_CAP, element_to_node


def _tag(html):
    return BeautifulSoup(html, "html.parser").find()


def test_basic_fields_and_class_id_extraction():
    tag = _tag('<div id="card-1" class="order-card js-card" data-x="42">'
               '<span>a</span><span>b</span>plain</div>')
    node = element_to_node(tag)
    assert node["tag"] == "div"
    assert node["id"] == "card-1"
    assert node["classes"] == ["order-card", "js-card"]
    assert node["attributes"] == {"data-x": "42"}  # id/class not duplicated here
    assert "attributes_truncated" not in node
    assert node["child_count"] == 2  # element children only, not the bare text node
    assert node["text"] == "a b plain"
    assert node["text_truncated"] is False
    assert node["html_length"] == len(str(tag))
    assert "html" not in node  # not included by default


def test_missing_id_is_null():
    assert element_to_node(_tag("<p>hi</p>"))["id"] is None


def test_attribute_value_truncated_with_flag_and_text_kept_short():
    long_val = "x" * (ATTR_CAP + 50)
    tag = _tag(f'<iframe name="{long_val}" title="ok"></iframe>')
    node = element_to_node(tag)
    assert len(node["attributes"]["name"]) == ATTR_CAP
    assert node["attributes"]["title"] == "ok"
    assert node["attributes_truncated"] == ["name"]


def test_text_truncated_reports_true_length():
    body = "word " * 1000  # ~5000 chars
    node = element_to_node(_tag(f"<div>{body}</div>"))
    collapsed_len = len(" ".join(body.split()))
    assert node["text_length"] == collapsed_len
    assert node["text_length"] > TEXT_CAP
    assert len(node["text"]) == TEXT_CAP
    assert node["text_truncated"] is True


def test_include_html_within_budget():
    tag = _tag("<b>hi</b>")
    node = element_to_node(tag, include_html=True)
    assert node["html"] == "<b>hi</b>"
    assert node["html_truncated"] is False


def test_include_html_truncated_to_max_bytes():
    tag = _tag("<div>" + "z" * 5000 + "</div>")
    node = element_to_node(tag, include_html=True, max_html_bytes=100)
    assert len(node["html"].encode("utf-8")) <= 100
    assert node["html_truncated"] is True
    assert node["html_length"] == len(str(tag))  # true length still reported
