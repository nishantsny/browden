from browden.dependencies.bs4 import BeautifulSoup
from browden.dom.serialize import ATTR_CAP, TEXT_CAP, element_to_node


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


def test_aria_labelledby_resolves_referenced_text():
    # The "a-button" pattern: a label-less submit control borrows its visible
    # name from an aria-hidden span via aria-labelledby. The name is resolved
    # (document-global) even though the referenced span is aria-hidden.
    html = ('<span class="a-button"><span class="a-button-inner">'
            '<input type="submit" class="a-button-input" aria-labelledby="btn-x">'
            '<span aria-hidden="true" id="btn-x">Continue</span>'
            '</span></span>')
    inp = BeautifulSoup(html, "html.parser").find("input")
    node = element_to_node(inp)
    assert node["labelledby_text"] == "Continue"
    assert node["text"] == ""  # the control itself carries no text


def test_aria_labelledby_multiple_idrefs_joined_in_order():
    html = ('<div><span id="a">Add</span><span id="b">to cart</span>'
            '<input type="submit" aria-labelledby="a b"></div>')
    inp = BeautifulSoup(html, "html.parser").find("input")
    assert element_to_node(inp)["labelledby_text"] == "Add to cart"


def test_no_labelledby_field_when_absent_or_dangling():
    # No attribute -> field omitted entirely (keeps the common node shape intact).
    assert "labelledby_text" not in element_to_node(_tag('<input type="submit">'))
    # Attribute present but pointing at a non-existent id -> resolves to "", omitted.
    inp = BeautifulSoup('<input type="submit" aria-labelledby="missing">',
                        "html.parser").find("input")
    assert "labelledby_text" not in element_to_node(inp)


def test_field_label_from_label_for_attribute():
    html = ('<div><label for="tip">Grocery Tip (optional):</label>'
            '<input id="tip" type="number"></div>')
    inp = BeautifulSoup(html, "html.parser").find("input")
    assert element_to_node(inp)["field_label"] == "Grocery Tip (optional):"


def test_field_label_from_wrapping_label():
    inp = BeautifulSoup('<label>Email <input type="email"></label>', "html.parser").find("input")
    assert element_to_node(inp)["field_label"] == "Email"


def test_no_field_label_when_unassociated_or_not_a_field():
    # Bare input whose "Grocery Tip" text is a mere sibling (no for=/wrapping label)
    # gets no field_label — matching the real Amazon tip input.
    inp = BeautifulSoup('<div><span>Grocery Tip</span><input id="t" type="number"></div>',
                        "html.parser").find("input")
    assert "field_label" not in element_to_node(inp)
    # Non-field elements never get a field_label.
    assert "field_label" not in element_to_node(_tag("<button>Go</button>"))


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


# -- labeled_control: what a <label> would activate (issue #146) --------------

VFS_PATTERN = '''
<div class="popup-wrapper">
  <p><input id="stage2" name="stage" type="radio" value="2">
     <label for="stage2">After Submitting a physical application</label></p>
</div>
'''


def _find(html, selector_name, **attrs):
    soup = BeautifulSoup(html, "html.parser")
    return soup.find(selector_name, attrs=attrs) if attrs else soup.find(selector_name)


def test_label_resolves_its_control_by_for_idref():
    node = element_to_node(_find(VFS_PATTERN, "label"))
    assert node["labeled_control"] == {"tag": "input", "attributes": {"type": "radio"}}
    assert node["text"] == "After Submitting a physical application"


def test_label_resolves_a_control_it_wraps():
    tag = _tag('<label>Remember me <input type="checkbox" name="rm"></label>')
    assert element_to_node(tag)["labeled_control"]["attributes"]["type"] == "checkbox"


def test_label_carries_only_the_attributes_the_gate_judges():
    tag = _tag('<label for="x">Pick</label>'
               '<input id="x" type="radio" disabled style="display:none" '
               'data-target-audience="ai-agent" value="secret" name="nope">')
    soup = BeautifulSoup(str(tag.parent), "html.parser")
    node = element_to_node(soup.find("label"))
    attrs = node["labeled_control"]["attributes"]
    assert attrs["type"] == "radio"
    assert attrs["disabled"] == ""
    assert attrs["style"] == "display:none"
    assert attrs["data-target-audience"] == "ai-agent"
    # not a DOM dump: unrelated attributes are left out
    assert "value" not in attrs and "name" not in attrs


def test_label_with_no_control_has_no_labeled_control_key():
    assert "labeled_control" not in element_to_node(_tag("<label>orphan</label>"))
    assert "labeled_control" not in element_to_node(_tag('<label for="ghost">x</label>'))


def test_non_label_elements_never_carry_labeled_control():
    assert "labeled_control" not in element_to_node(_tag('<input type="radio" id="r">'))
    assert "labeled_control" not in element_to_node(_tag("<button>Go</button>"))


def test_for_idref_wins_over_a_wrapped_control():
    html = ('<div><input id="outer" type="checkbox">'
            '<label for="outer">Pick <input type="text" id="inner"></label></div>')
    node = element_to_node(BeautifulSoup(html, "html.parser").find("label"))
    assert node["labeled_control"]["attributes"]["type"] == "checkbox"
