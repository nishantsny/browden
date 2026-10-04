# Reading the DOM: response shape and caps

The four read tools — `get_element_by_id`, `get_elements_by_class_name`,
`query_selector`, `query_selector_all` — return *serialized nodes*, not raw HTML.
Every one of them truncates, and the caps are not something you should have to
infer from a response. This page is the reference for what comes back, what is
capped, and how to get a whole large node out of a page.

Why cap at all: a tool response is fed straight into a model's context. One
Amazon order card is ~355 chars of text but ~167 KB of HTML, and a single
attribute (an ad iframe's `name`) can hold 15 KB of JSON. So raw HTML and raw
attribute values are never echoed by default — browden truncates, reports the
true size, and lets the caller opt into more. The caps live in
[`browden/dom/serialize.py`](../browden/dom/serialize.py).

## The caps

| What | Cap | How you can tell | Can it be raised? |
| --- | --- | --- | --- |
| `text` | **2000 chars** | `text_length` (true size), `text_truncated` | **No.** Not by any parameter, and it cannot be paginated. Use `include_html` instead. |
| each attribute value | 256 chars | `attributes_truncated` lists the keys that were cut | No |
| `html` | `max_html_bytes`, default **4096 bytes** | `html_truncated` | Yes — raise `max_html_bytes`. Only present when `include_html=True`. |
| elements per call (list tools) | `limit`, default 10, **clamped to 50** | `returned`, `total_count`, `next_offset` | Up to 50; page through the rest with `offset` |

Two consequences worth stating outright, because both have cost real debugging
time:

- **`max_html_bytes` does nothing unless `include_html=True`.** It is a cap on
  the `html` field, and `html` is opt-in. Passing a large `max_html_bytes` on
  its own changes nothing, and the 2000-char `text` you get back looks like the
  value you passed was ignored or clamped.
- **`limit` / `offset` paginate *elements*, not the content of one element.**
  There is no pagination for the text inside a single node.

## Reading a large payload out of a page

The case: the useful payload on the page is one big node — a JSON endpoint
rendered in Chrome's `<pre>`, a `<script type="application/json">` blob, a long
article body.

```jsonc
// ✗ Looks like the tool clamps output. include_html defaults to false, so
//   max_html_bytes is inert and you get the unconditional 2000-char text.
query_selector("pre", id=tab, max_html_bytes=5000000)
→ {"found": true, "element": {"text_length": 698676, "text_truncated": true,
                              "text": "…2000 chars…"}}

// ✓ Ask for the HTML, with a byte cap big enough to hold the node.
query_selector("pre", id=tab, include_html=true, max_html_bytes=5000000)
→ {"found": true, "element": {"html_truncated": false, "html": "<pre>…698 KB…</pre>"}}
```

**Assert `html_truncated == false`** rather than trusting the length you got
back: `max_html_bytes` cuts the UTF-8 *bytes* and decodes with `errors="ignore"`,
so a truncated `html` is a valid string that silently ends early — and if it was
a JSON blob, it will no longer parse.

If the node is genuinely larger than you want in context, read it in one call
with a large `max_html_bytes` and write it to a file for processing, rather than
trying to walk it with repeated small reads.

## Response shape

Both single-element tools (`get_element_by_id`, `query_selector`) return:

```jsonc
{
  "id": "<tab id>",
  "reloaded": false,     // true if the cached DOM snapshot had expired and the tab was reloaded
  "found": true,
  "element": { /* node, see below */ }   // null when found=false
}
```

A missing element is `found: false`, **not** an error. (Invalid CSS *is* an
error: `{"error": "invalid CSS selector: …", "id": …}`.)

Both list tools (`get_elements_by_class_name`, `query_selector_all`) return:

```jsonc
{
  "id": "<tab id>",
  "reloaded": false,
  "total_count": 137,    // matches in the whole document, not just this page
  "offset": 0,
  "limit": 10,           // the EFFECTIVE limit after clamping to 1..50
  "returned": 10,
  "next_offset": 10,     // null exactly when pagination is exhausted
  "elements": [ /* nodes */ ]
}
```

### The node

| Field | Always present | What it is |
| --- | --- | --- |
| `tag` | ✓ | Tag name, lowercase (`"div"`, `"input"`) |
| `id` | ✓ | The element's `id`, or `null` |
| `classes` | ✓ | List of class names |
| `attributes` | ✓ | Every other attribute, as a flat `str → str` map. Multi-valued attributes are space-joined. Each value capped at 256 chars. `id` and `class` are excluded — they have their own fields. |
| `text` | ✓ | Collapsed visible text (whitespace runs → one space), capped at 2000 chars |
| `text_length` | ✓ | The **true** length before the cap |
| `text_truncated` | ✓ | Whether `text` was cut |
| `html_length` | ✓ | The true length of the outer HTML, in chars — reported even when `include_html=False`, so you can size a follow-up `max_html_bytes` |
| `child_count` | ✓ | Direct element children |
| `attributes_truncated` | only if some were | List of attribute keys whose values were cut |
| `labelledby_text` | only if `aria-labelledby` resolves | The accessible name contributed by `aria-labelledby`, resolved document-globally — the pattern where a button's visible glyphs live in a separate `<span>` |
| `field_label` | only for a labelled form field | The text of the field's associated `<label>` |
| `html` | only with `include_html=True` | Outer HTML, cut to `max_html_bytes` bytes |
| `html_truncated` | only with `include_html=True` | Whether `html` was cut |

`labelledby_text` and `field_label` are not decoration: they are what the write
gates match a host's `label` regex against, so they tell you the string an
operator must authorize to let `click` or `insert_text` touch that element.
