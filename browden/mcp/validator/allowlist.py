import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .popularity import PopularityAllowlist
from .tranco import DEFAULT_TOP_N, TRANCO_FILENAME, canonical_host

# Defaults for the `infra` section. They live here, with the rest of the config
# layer, so there is exactly one place that says what an unset knob means; the
# session layer imports the reap default rather than restating it.
DEFAULT_MAX_BROWSER_SESSIONS = 10
DEFAULT_MAX_TABS_PER_SESSION = 20
# How often the idle reaper wakes. Deliberately coarse: sweeping is the only
# cleanup pass (tools never sweep), and a tab idle for an hour can wait.
DEFAULT_REAP_INTERVAL_SECONDS = 7200

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
    ActionAllowlist path, via :func:`_coerce_page_rules`) passes through.
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


# Top-level keys that are not write actions. Every *other* key in a rule set is
# one (see PolicySet), so this is the single list that says "handled elsewhere":
# `infra` is process-wide (ActionAllowlist owns it), `read` builds the read gate,
# `denylist` the always-deny list.
_NON_ACTION_SECTIONS = ("infra", "read", "denylist")


class PolicySet:
    """The rules that decide one request: a denylist, the read gate, write actions.

    This is the *evaluated* surface the gates use — everything that answers "may
    this action touch this host and page". It is built from a mapping of rule
    sections, whose keys are handled as follows:

    * ``denylist`` — host -> path regexes that are **always refused** (checked
      first, wins over everything). ``*`` host matches any host.
    * ``read`` — the read/navigate gate, built into a :class:`ReadPolicy`::

          read:
            enabled: true                 # master switch for the read allowlist
            tranco: {enabled: true, top_n: 1000000}
            website_overrides:
              "*": [".*"]                 # host -> path regexes; "*" = any host

    * any other key (e.g. ``click``) — a *write action*. A host maps either to a
      single **legacy** mapping (a host-wide rule) or to an ordered **list of
      page rules**, each governing only the pages its ``path`` selector matches::

          click:
            amazon.com:                    # page-scoped: label differs per page
              - path: ['^/(dp|gp/product)/.*']
                label: '(?i)add to cart'   # required; '.*' allows any control
              - path: ['^/gp/buy/.*']
                match_on: path             # 'path' (default) | 'url' (+query+fragment)
                label: '(?i)(continue|place your order)'
            ebay.com:                      # legacy host-wide form still valid
              label: '(?i)add to (cart|basket)'
              paths: [".*"]

      A click is authorized when SOME matching page rule's ``label`` matches the
      control — the rules are additive. The label is mandatory so that allowing
      every control reads explicitly as ``label: '.*'``, never as a silent
      default. ``match_on: url`` matches ``path?query#fragment`` — the only way to
      scope a page on a hash-routed SPA, where every page shares the path ``/``.
    * ``write-text`` — the text-entry write action (the ``insert_text`` tool), a
      section *separate* from ``click`` so permitting typing never implies
      permitting clicks. Same page-rule shape, but the ``label`` is matched
      against the **field's visible label** — its placeholder, aria-label,
      resolved ``aria-labelledby`` / ``<label>``, or title — so an operator
      authorizes *which* text boxes may be typed into by the name a human reads
      next to them. A page rule may additionally list ``field_ids``: exact ``id``
      / ``name`` values that authorize a text box carrying **no visible label**
      (e.g. Amazon's Fresh grocery-tip input), which the label regex can never
      match. That trusts a non-visible identifier, so it is opt-in per rule and
      page-scoped — the id is typable only on the pages that rule matches::

          write-text:
            amazon.com:
              - path: ['^/gp/css/order-history.*']
                label: '(?i)grocery tip.*'
                field_ids: [tip-widget--edit-form--amount-input]

    ``infra`` is ignored here — session/tab caps are process-wide, not part of any
    access decision, and belong to the :class:`ActionAllowlist` that owns this set.

    ``read_policy`` gates reads; ``denylist`` is the always-deny list (also
    consulted by write actions); ``section(name)`` and ``rules_for(name, ...)``
    gate write actions. An unlisted write action default-denies.
    """

    def __init__(self, sections: dict[str, object], tranco_path: Path | None = None):
        # tranco_path is the Tranco snapshot that sits next to the allowlist
        # file; the loader/from_file pass it in. A bare dict construction (tests,
        # the import-time default) leaves it None -> the ~/.browden fallback.
        self._sections: dict[str, Allowlist] = {}
        # action -> canonical host -> ordered page rules (label + field_ids).
        self._rules: "dict[str, dict[str, list[PageRule]]]" = {}
        # A denylist blocks broadly: prefix match, not fullmatch (see Allowlist).
        self._denylist = Allowlist.create_denylist(
            _coerce_section(sections.get("denylist"), want_label=False, where="denylist"))
        self._read_policy = self._build_read_policy(
            sections.get("read"), self._denylist, tranco_path)
        for action, rules in sections.items():
            if action in _NON_ACTION_SECTIONS:
                continue
            host_rules = _coerce_section(rules, want_label=True, where=action)
            self._rules[action] = {canonical_host(h): rs for h, rs in host_rules.items()}
            # section() keeps a page-admission view (labels ignored) for callers
            # that only ask "may this action touch this host+page at all".
            self._sections[action] = Allowlist.create_allowlist(host_rules)

    @staticmethod
    def _build_read_policy(read_cfg: object, denylist: Allowlist,
                           tranco_path: Path | None) -> ReadPolicy:
        """Assemble the ReadPolicy from the ``read`` block (fail-closed if absent).

        With no ``read`` block the allowlist is enabled but empty, so only
        denylist + (nothing) applies — every read is denied until the operator
        opts sites in. The shipped sample enables Tranco so it works out of box.

        ``tranco_path`` is the snapshot sitting next to the allowlist config; if
        it is missing we fall back to the ~/.browden default (``path=None``), so
        a snapshot that setup has not fetched yet degrades to "Tranco matches
        nothing" rather than a crash.
        """
        cfg = read_cfg if isinstance(read_cfg, dict) else {}
        enabled = bool(cfg.get("enabled", True))
        tranco_cfg = cfg.get("tranco") or {}
        tranco = None
        if tranco_cfg.get("enabled"):
            snapshot = tranco_path if (tranco_path and tranco_path.exists()) else None
            tranco = PopularityAllowlist(tranco_top_n=int(tranco_cfg.get("top_n", DEFAULT_TOP_N)), path=snapshot)
        overrides = Allowlist.create_allowlist(
            _coerce_section(cfg.get("website_overrides"), want_label=False,
                            where="read.website_overrides"))
        return ReadPolicy(enabled=enabled, tranco=tranco, overrides=overrides, denylist=denylist)

    @property
    def read_policy(self) -> ReadPolicy:
        """The read/navigate gate (denylist + master switch + Tranco + overrides)."""
        return self._read_policy

    @property
    def denylist(self) -> Allowlist:
        """The always-deny list, so write actions can veto denied hosts too."""
        return self._denylist

    def is_denied(self, host: str, path: str) -> bool:
        """True if ``(host, path)`` is on the denylist (refused for every action)."""
        return self._denylist.is_allowed(host, path)

    def section(self, action: str) -> Allowlist:
        """Return the host/page-admission allowlist for ``action`` (labels ignored);
        an empty (deny-all) one if unlisted."""
        return self._sections.get(action) or Allowlist.create_allowlist({})

    def rules_for(self, action: str, host: str, path: str,
                  query: str = "", fragment: str = "") -> list[PageRule]:
        """The page rules for ``action`` on ``host`` that match this page, in order.

        Empty if the action/host is unlisted or no rule's page selector matches —
        the write gates read that as "this action is not authorized on this page"
        and refuse before touching the DOM. Each returned rule carries the
        ``label`` and ``field_ids`` that authorize a control *on these pages*, so
        the caller checks the live element against the union of them.
        """
        by_host = self._rules.get(action)
        if not by_host:
            return []
        rules = by_host.get(canonical_host(host)) or by_host.get("*")
        if not rules:
            return []
        return [r for r in rules
                if r.matches_page(path, query, fragment, full_match=True)]


class ActionAllowlist:
    """A whole loaded config: the process-wide ``infra`` caps plus the rule sets.

    Two kinds of setting live in an allowlist file and they behave differently,
    so they are held apart here:

    * ``infra`` — ``max_browser_sessions`` / ``max_tabs_per_session`` /
      ``reap_interval_seconds``. Process-wide resource caps, read by the session
      layer; they gate no access decision and belong to no single request.
    * everything else — ``denylist``, ``read``, and the write actions: the rules
      that decide whether a given request may act. They are parsed into a
      :class:`PolicySet` (which documents the grammar), reachable as
      :attr:`policy`.

    :attr:`policy` is the only way to a decision: a gate is always handed the
    rule set that governs the request it is deciding, never the container.
    """

    def __init__(self, sections: dict[str, object], tranco_path: Path | None = None):
        # tranco_path is the Tranco snapshot that sits next to the allowlist
        # file; the loader/from_file pass it in. A bare dict construction (tests,
        # the import-time default) leaves it None -> the ~/.browden fallback.
        self.max_browser_sessions = DEFAULT_MAX_BROWSER_SESSIONS
        self.max_tabs_per_session = DEFAULT_MAX_TABS_PER_SESSION
        self.reap_interval_seconds = DEFAULT_REAP_INTERVAL_SECONDS
        infra = sections.get("infra")
        if isinstance(infra, dict):
            self.max_browser_sessions = int(
                infra.get("max_browser_sessions", DEFAULT_MAX_BROWSER_SESSIONS))
            self.max_tabs_per_session = int(
                infra.get("max_tabs_per_session", DEFAULT_MAX_TABS_PER_SESSION))
            self.reap_interval_seconds = int(
                infra.get("reap_interval_seconds", DEFAULT_REAP_INTERVAL_SECONDS))
        self._policy = PolicySet(sections, tranco_path)

    @classmethod
    def from_file(cls, path: Path) -> "ActionAllowlist":
        return cls(yaml.safe_load(path.read_text()) or {},
                   tranco_path=path.parent / TRANCO_FILENAME)

    @property
    def policy(self) -> PolicySet:
        """The rule set every gate decides against."""
        return self._policy
