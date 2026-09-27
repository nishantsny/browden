import re
from dataclasses import dataclass

from .popularity import PopularityAllowlist
from .tranco import canonical_host

# The write actions browden gates, by config section name: `click` (the click
# tool), `write-text` (insert_text) and `press-key` (press_key). The schema
# refuses any other top-level section, so a new write tool is added here before
# a config can authorize it.
WRITE_ACTIONS = ("click", "write-text", "press-key")

# canonical_host is imported (not redefined) so the denylist/overrides normalize
# hosts identically to the Tranco check — a trailing dot or leading www. must not
# make the two gates disagree (finding H3).


_ENCODED_DOT = re.compile(r"%2e", re.IGNORECASE)


def _normalize_path(path: str) -> str:
    """Resolve a URL path to the form Chrome will actually request.

    Chrome decodes an encoded dot (``%2e`` -> ``.``) and removes ``.`` / ``..``
    segments *before* issuing the request, so a path-scoped allow/deny rule has
    to match against that resolved form. Otherwise ``/./checkout``,
    ``/x/../checkout`` and ``/%2e/checkout`` all slip past a rule written for
    ``/checkout`` (a denylist entry is evaded; a path-scoped allow is escaped).

    We decode *only* the dot encoding, not the whole path: Chrome keeps other
    percent-escapes (notably ``%2f``) encoded, so a blanket unquote would diverge
    from the browser in the other direction. A leading and trailing slash are
    preserved because both are significant to the regexes rules are written with.
    """
    decoded = _ENCODED_DOT.sub(".", path)
    leading = decoded.startswith("/")
    trailing = decoded.endswith("/")
    out: list[str] = []
    for seg in decoded.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    normalized = "/".join(out)
    if leading:
        normalized = "/" + normalized
    if trailing and not normalized.endswith("/"):
        normalized += "/"
    return normalized or "/"


_MATCH_MODES = ("path", "url")


def _compose_target(path: str, query: str, fragment: str, match_on: str) -> str:
    """The string a page rule's regexes are tested against.

    ``match_on: path`` (the default) yields just the resolved path, so a rule is
    written exactly the way path rules always were. ``match_on: url`` appends the
    query and fragment (``/checkout?step=2#pay``) — the only way to tell pages
    apart on a hash-routed SPA, where every page shares the path ``/`` and
    differs only in the ``#/...`` fragment. The path portion is normalized either
    way (dot segments collapsed, ``%2e`` decoded), so the evasions
    :func:`_normalize_path` closes for a path rule can't reopen through the url
    form.
    """
    target = _normalize_path(path or "/")
    if match_on == "url":
        if query:
            target += "?" + query
        if fragment:
            target += "#" + fragment
    return target


@dataclass(frozen=True)
class PageRule:
    """One page-scoped rule: which pages it governs, plus — for a write action —
    the control ``label`` and label-less ``field_ids`` it authorizes ON those
    pages (rather than host-wide, as before).

    ``patterns`` are tested against the page selector built by
    :func:`_compose_target` under ``match_on``. ``label`` is the required
    visible-text / field-label regex for a write action, and ``None`` for a read
    override or denylist rule (which have no label). ``field_ids`` are exact
    ``id``/``name`` values that authorize a label-less text box — page-scoped
    now, so an id is typable only on the pages this rule matches.
    """
    patterns: "tuple[re.Pattern[str], ...]"
    match_on: str
    label: "re.Pattern[str] | None" = None
    field_ids: "frozenset[str]" = frozenset()
    # `press-key` only: the control keys this rule authorizes on its pages (W3C
    # `key` values, e.g. {"Enter", "ArrowDown"}). Empty for every other action —
    # and a press-key rule with an empty set authorizes nothing (fail-closed).
    keys: "frozenset[str]" = frozenset()

    def matches_page(self, path: str, query: str = "", fragment: str = "",
                     *, full_match: bool) -> bool:
        """True if this rule governs ``(path, query, fragment)``.

        ``full_match`` mirrors :class:`Allowlist`: an allow rule fullmatches (so
        ``^/products`` does not also admit ``/products-secret-admin``); a deny
        rule prefix-matches (so ``^/checkout`` still blocks ``/checkout/pay``).
        """
        target = _compose_target(path, query, fragment, self.match_on)
        return any(
            (p.fullmatch(target) if full_match else p.match(target))
            for p in self.patterns
        )


def _as_pattern_tuple(path: object) -> "tuple[re.Pattern[str], ...]":
    """Compile a page-selector spec (a single regex string or a list of them)."""
    if isinstance(path, str):
        path = [path]
    return tuple(re.compile(p) for p in path)


def _page_rule_from_mapping(rule: dict, *, want_label: bool, where: str) -> PageRule:
    """Build one :class:`PageRule` from a mapping ``{path, match_on?, label?, field_ids?}``.

    ``paths`` (the legacy write key) and ``path`` (the page-rule key) are synonyms
    for the page selector, defaulting to any page. ``want_label`` is true for the
    write actions, where a ``label`` is mandatory; it is forbidden elsewhere.
    """
    match_on = rule.get("match_on", "path")
    if match_on not in _MATCH_MODES:
        raise ValueError(f"{where}: match_on must be 'path' or 'url', got {match_on!r}")
    raw = rule.get("path", rule.get("paths", [".*"]))
    label = None
    if want_label:
        if "label" not in rule:
            raise ValueError(
                f"{where}: a write-action rule requires a 'label' regex "
                f"(use '.*' to allow any control)")
        label = re.compile(rule["label"])
    elif "label" in rule:
        raise ValueError(f"{where}: 'label' is only meaningful for a write action")
    field_ids = frozenset(str(x) for x in (rule.get("field_ids") or []))
    # `keys` is only meaningful for the press-key action; other actions never set
    # it. Parsed generically here (kept as authored strings) — the press-key gate
    # is what enforces "must be a control key" and "must authorize the pressed key".
    keys = frozenset(str(k) for k in (rule.get("keys") or []))
    return PageRule(patterns=_as_pattern_tuple(raw), match_on=match_on,
                    label=label, field_ids=field_ids, keys=keys)


def _coerce_page_rules(spec: object, *, want_label: bool, where: str) -> list[PageRule]:
    """Normalize a host's rule spec into an ordered list of :class:`PageRule`.

    Three input shapes are accepted, so every pre-existing config keeps loading:

    * **write legacy** — a single mapping ``{label, paths?, field_ids?}`` (a
      host-wide rule): one PageRule with ``match_on: path``.
    * **read / deny legacy** — a bare list of path regexes ``["^/docs/.*"]``: one
      label-less PageRule with ``match_on: path``.
    * **page-rule list** — a list of mappings, each
      ``{path, match_on?, label?, field_ids?}``: one PageRule apiece, in order,
      so a host can scope *different* labels / fields to *different* pages.

    ``want_label`` is true for the write actions (``click`` / ``write-text``):
    every rule must then carry a ``label`` (a bare path-string list is rejected —
    what a control may do is never implicit). Read overrides and the denylist
    pass ``want_label=False``.
    """
    if isinstance(spec, dict):  # write legacy: one host-wide mapping.
        return [_page_rule_from_mapping(spec, want_label=want_label, where=where)]
    if not isinstance(spec, list) or not spec:
        return []
    if all(isinstance(x, str) for x in spec):  # read/deny legacy: [path regex, ...].
        if want_label:
            raise ValueError(
                f"{where}: a write action needs page rules with a 'label', not a bare "
                f"path list (write host-wide as {{label: ..., paths: [...]}})")
        return [PageRule(patterns=_as_pattern_tuple(spec), match_on="path")]
    return [_page_rule_from_mapping(r, want_label=want_label, where=f"{where}[{i}]")
            for i, r in enumerate(spec)]


def _coerce_section(rules: object, *, want_label: bool, where: str) -> "dict[str, list[PageRule]]":
    """Coerce a whole host -> spec section into host -> [PageRule]."""
    return {host: _coerce_page_rules(spec, want_label=want_label, where=f"{where}.{host}")
            for host, spec in (rules or {}).items()}


def _as_page_rules(page_rules: object) -> list[PageRule]:
    """Accept either a ready ``[PageRule]`` or the legacy ``[path regex]`` list.

    Direct :class:`Allowlist` construction (and its tests) still pass a bare list
    of path-regex strings; wrap that into a single ``match_on: path`` rule so the
    class has one internal representation. A list already holding PageRules (the
    runtime-configuration path, via :func:`_coerce_page_rules`) passes through.
    """
    rs = list(page_rules or [])
    if rs and all(isinstance(x, str) for x in rs):
        return [PageRule(patterns=_as_pattern_tuple(rs), match_on="path")]
    return rs


class Allowlist:
    """Per-host page-rule allowlist. Use host key '*' for a wildcard fallback."""

    def __init__(self, rules: "dict[str, list[PageRule]]", *, full_match: bool):
        # full_match decides how a page regex is applied. An *allow* list
        # fullmatches (the pattern must span the whole target) so `^/products`
        # does not also wave through `/products-secret-admin`. A *denylist* is the
        # opposite risk — it should block broadly — so it keeps prefix semantics
        # (start-anchored `re.match`): `^/checkout` still denies `/checkout/pay`.
        #
        # Keys are canonicalized the SAME way lookups are (see is_allowed), so a
        # rule written 'www.x.com' or 'x.com.' matches host 'x.com' and vice versa
        # — no silently-inert denylist entries, no trailing-dot bypass (H3).
        self._full_match = full_match
        self._rules: "dict[str, list[PageRule]]" = {
            canonical_host(host): _as_page_rules(page_rules)
            for host, page_rules in rules.items()
        }

    @classmethod
    def create_allowlist(cls, rules: "dict[str, list[PageRule]]") -> "Allowlist":
        """An ALLOW list: each page regex must fullmatch the whole target, so
        ``^/products`` does not also admit ``/products-secret-admin`` (spell a
        prefix rule as ``^/products/.*``)."""
        return cls(rules, full_match=True)

    @classmethod
    def create_denylist(cls, rules: "dict[str, list[PageRule]]") -> "Allowlist":
        """A DENY list: each page regex prefix-matches (start-anchored) so it
        blocks broadly — ``^/checkout`` still denies ``/checkout/pay``."""
        return cls(rules, full_match=False)

    def is_allowed(self, host: str, path: str, query: str = "", fragment: str = "") -> bool:
        rules = self._rules.get(canonical_host(host)) or self._rules.get("*")
        if not rules:
            return False
        # A rule matches per its own match_on (path vs. path+query+fragment); the
        # host is allowed if ANY of its rules does. Additive by construction, so
        # adding a rule can only widen — narrowing is the denylist's job.
        return any(r.matches_page(path, query, fragment, full_match=self._full_match)
                   for r in rules)

    def covers(self, host: str) -> bool:
        """True if a rule set governs ``host`` (an exact entry or the ``*`` wildcard).

        Distinct from ``is_allowed``: a host can be *covered* (has rules) yet be
        denied because its path doesn't match. Lets the read policy give an
        explicit override precedence over Tranco even when it path-scopes a host.
        """
        return canonical_host(host) in self._rules or "*" in self._rules

    def has_host(self, host: str) -> bool:
        """True if an explicit (non-wildcard) entry names ``host``.

        Unlike :meth:`covers` this ignores the ``*`` fallback — it answers "did
        the operator name *this* host specifically", which the scheme gate uses
        to decide whether a non-https scheme was deliberately opted in for it.
        """
        return canonical_host(host) in self._rules


class ReadPolicy:
    """The read/navigate gate: a denylist veto plus two ways to be allowed.

    A ``(host, path)`` is decided in this fixed order:

    1. **denylist** — if it matches, the URL is refused outright (wins over
       everything, even when the allowlist is disabled).
    2. **master switch** — if ``enabled`` is false, every non-denied URL is
       allowed (the read allowlist is off).
    3. **website_overrides** — an explicit rule for the host (or the ``*``
       wildcard) *triumphs over Tranco*: once a host is covered here, its
       override alone decides, so you can both allow a host Tranco doesn't rank
       **and** path-scope (or effectively block) one that Tranco would otherwise
       wave through.
    4. **Tranco** — for hosts with no override, allowed if the host is in the
       fetched top-sites snapshot (kept next to the allowlist config).

    Otherwise it is denied (default-deny).
    """

    def __init__(self, *, enabled: bool, tranco: PopularityAllowlist | None,
                 overrides: Allowlist, denylist: Allowlist):
        self._enabled = enabled
        self._tranco = tranco
        self._overrides = overrides
        self._denylist = denylist

    def is_allowed(self, host: str, path: str, query: str = "", fragment: str = "") -> bool:
        if self._denylist.is_allowed(host, path):
            return False
        if not self._enabled:
            return True
        # An explicit override for this host wins over Tranco — even to restrict
        # (page-scope) a host Tranco would otherwise allow wholesale. Listing a
        # host here therefore DEMOTES it from Tranco's blanket grant to exactly
        # the pages its override rules match (query/fragment honored when a rule
        # opts into match_on: url — needed for hash-routed SPAs).
        if self._overrides.covers(host):
            return self._overrides.is_allowed(host, path, query, fragment)
        return self._tranco is not None and self._tranco.contains(host)

    def override_has_host(self, host: str) -> bool:
        """True if the read overrides name ``host`` explicitly (not via ``*``).

        The scheme gate (see :func:`validate_url`) consults this: a non-https URL
        (``file://``, plaintext ``http://``, …) is accepted only for a host the
        operator has *explicitly* overridden. So opening the web wholesale with
        ``website_overrides: {"*": [".*"]}`` does **not** silently re-enable
        ``file://`` or plaintext http everywhere — you name the host to opt it in
        (e.g. ``localhost: [".*"]`` for http, or ``"": ["^/home/me/.*"]`` for
        file:// paths, which carry an empty host).
        """
        return self._overrides.has_host(host)
