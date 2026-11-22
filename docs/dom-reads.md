# Reading the DOM: response shape and caps

The four read tools — `get_element_by_id`, `get_elements_by_class_name`,
`query_selector`, `query_selector_all` — return *serialized nodes*, not raw HTML.
Every one of them truncates, and the caps are not something you should have to
infer from a response. This page is the reference for what comes back, what is
capped, how to get a whole large node out of a page, and
[which version of the page a read sees](#the-dom-cache-which-page-a-read-sees).

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

## The DOM cache: which page a read sees

The read tools don't query the live page. Each tab has one cached snapshot, the
page's HTML parsed at one instant, and a read answers from it. A read that finds
no snapshot takes one; every read after that reuses it. What drops or replaces
the snapshot is fixed:

| What happened | The tab's snapshot |
| --- | --- |
| `navigate` | Dropped. The next read takes a new one. |
| `click`, `insert_text`, `press_key`, `upload_file` | Dropped once the action is attempted, including when it then fails partway (the page may already have changed). The next read takes a new one. A write that a gate refuses did nothing to the page and leaves the snapshot alone. |
| `switch_to_frame`, `switch_to_parent_frame`, `switch_to_default_content` | Dropped. The next read snapshots the document now in focus. |
| `invalidate_dom_cache` | Dropped, and nothing else happens: the page is not touched. |
| `force_reload_tab` | The page is reloaded and the snapshot retaken in the same call. |
| The page's own JS changes its URL | Stops answering: a snapshot answers only for the URL it was taken from, so a read at the new URL takes a new one. |
| The page's own JS changes the DOM, same URL | **Kept**, if there is one: browden does not watch the page, so reads go on answering from the snapshot taken before the change. If the snapshot was already dropped (the change came after a write and before the next read), that read fetches the live page and sees it. |
| The snapshot is 1 hour old | The next read reloads the tab first and reports `reloaded: true`. |

`screenshot` never uses the snapshot. It captures the live pixels.

### After a write, just read

A write needs no follow-up to make its effect readable. The snapshot is already
gone, so the next read fetches the live page:

```jsonc
click("#add-to-cart", id=tab)
query_selector("#cart-count", id=tab)   // fetches the live page, so it sees what the click did
```

That read sees the page as it is when the read runs: what the click did, and
whatever the page has done on its own since.

The write drops the snapshot and leaves rebuilding it to the next read on
purpose. A snapshot taken inside the write would show the instant after the
action, before the page has reacted: the request a click sends hasn't come back
and the modal it opens hasn't rendered. The read that follows comes later, so it
sees more.

That first read is also the last one to look at the live page until something
in the table happens again. On a page that reacts asynchronously, a read made
before the reaction has rendered snapshots the in-between state, and reading
again returns the same snapshot however long you wait. To poll for a late
change, drop the snapshot before each read:

```jsonc
click("#save", id=tab)
query_selector(".toast", id=tab)        // found: false, not rendered yet. That is now the snapshot.
query_selector(".toast", id=tab)        // ✗ same snapshot, same answer
invalidate_dom_cache(id=tab)
query_selector(".toast", id=tab)        // ✓ fetches the live page again
```

Use `invalidate_dom_cache` for this, not `force_reload_tab`. A reload discards
what the page built in the live DOM (the open modal, the half-filled form),
aborts requests still in flight (the save the click just started), and retakes
the snapshot as soon as the page has loaded, which on a client-rendered page can
be before its content is there.

### Writes match the live page

`click`, `insert_text`, `press_key` and `upload_file` never consult the
snapshot. Their `css_selector` is matched against the live DOM at the moment of
the write. So a selector taken from a read can fail to match if the page
re-rendered after that read, since a framework may give the element a new
generated id each time it renders. When a write reports
`no element matches selector` for an element a read just returned, call
`invalidate_dom_cache`, read again, and use the selector from the fresh read.

## Response shape

Both single-element tools (`get_element_by_id`, `query_selector`) return:

```jsonc
{
  "id": "<tab id>",
  "reloaded": false,     // true if the cached DOM snapshot had expired (1 hour) and the tab was reloaded
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
