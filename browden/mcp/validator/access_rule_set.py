"""The access rules a request is decided by: denylist, read gate, write actions.

:class:`BrowdenAccessRuleSet` is the evaluated rule surface every gate consults.
It is built from the rule sections of a loaded config and holds nothing
process-wide — the ``infra`` caps live on
:class:`~.runtime_configuration.BrowdenRuntimeConfiguration`, which owns one of these.
"""
import re
from pathlib import Path

from .allowlist import WRITE_ACTIONS, HostRuleMatcher, PageRule, ReadPolicy, _coerce_section
from .intent import ACTIVATION_KEYS
from .popularity import PopularityAllowlist
from .tranco import DEFAULT_TOP_N, canonical_host

# The rule an `allow_all` set answers every write-action lookup with: any page,
# any control, and — for press-key — every control key the gate would accept.
# Deliberately NOT a `"*"` read override: see BrowdenAccessRuleSet's `allow_all`.
_ANY_PAGE_ANY_CONTROL = PageRule(patterns=(re.compile(".*"),), match_on="url",
                                 label=re.compile(".*"), keys=ACTIVATION_KEYS)


def _merge_host_rules(base: "dict[str, list[PageRule]]",
                      extra: "dict[str, list[PageRule]]") -> "dict[str, list[PageRule]]":
    """Union two coerced ``host -> [PageRule]`` maps; a host in both keeps both.

    Rules are additive by construction (a host is allowed if ANY of its rules
    matches — see :meth:`HostRuleMatcher.is_allowed`), so a union is exactly "the
    profile's rules on top of the global floor". Host keys are canonicalized
    here, before the union, or ``amazon.com`` in one map and ``www.amazon.com``
    in the other would look like two hosts and one of them would be dropped when
    the merged map is canonicalized later.
    """
    merged: "dict[str, list[PageRule]]" = {}
    for rules in (base, extra):
        for host, page_rules in rules.items():
            merged.setdefault(canonical_host(host), []).extend(page_rules)
    return merged


def _merge_read_cfg(base: dict, extra: dict) -> dict:
    """Merge two ``read`` blocks — the profile's settings over the global ones.

    Scalars (``enabled``) and each ``tranco`` knob are taken from the profile
    when it states them and inherited when it doesn't, so a profile can turn the
    popularity net off (or move ``top_n``) without restating the rest.
    ``website_overrides`` is absent from the result: it is a rule *map*, unioned
    by :func:`_merge_host_rules` in coerced form rather than merged raw here.
    """
    merged = {**base, **extra}
    tranco = {**(base.get("tranco") or {}), **(extra.get("tranco") or {})}
    if tranco:
        merged["tranco"] = tranco
    merged.pop("website_overrides", None)
    return merged


class BrowdenAccessRuleSet:
    """The access rules any single request is decided by: a denylist, the read
    gate, and the write actions.

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

    * ``click`` — a *write action*. A host maps either to a single **legacy**
      mapping (a host-wide rule) or to an ordered **list of page rules**, each
      governing only the pages its ``path`` selector matches::

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

    * ``allow_all: true`` — every write action on every page, and reads of every
      host the read gate would otherwise *rank*. It is the sugar for "this is a
      scratch profile: let the agent work in it", so an operator can scope down
      to a fresh profile dir and browse without a config edit per host. The
      schema admits it only inside a ``profiles`` entry.

      It is deliberately **not** shorthand for ``website_overrides: {"*": [".*"]}``:
      an override that covers a host short-circuits :meth:`ReadPolicy.is_allowed`
      *before* the Tranco branch, so spelling it that way would silently switch
      the popularity net off — the opposite of the default this wants. Under
      ``allow_all`` the Tranco check still runs, and is switched *on* for the set
      unless it says otherwise, so broad browsing covers the established web
      while an unranked host — a typosquat, a domain registered yesterday, a
      paste site an agent followed a link to — still needs a deliberate act::

          allow_all: true
          read:
            tranco: {enabled: false}    # deliberate; the net is on until you say this

      Inherited page-scopes do not narrow it: a global override that demotes a
      host to a few pages still *adds* that host everywhere, but inside an
      ``allow_all`` set the rest of that host is decided by the popularity check
      like any other, not refused by a rule the profile never asked for.

      Two things it does not touch. The **denylist** still wins (it is checked
      first, and the global one is unioned in). And the **scheme gate** is
      unmoved: ``file://`` and plaintext ``http://`` are admitted only for a host
      named *explicitly* in ``website_overrides``, which ``allow_all`` never
      does — opting into local file reads stays a named-host act.

    Write actions are read by name from :data:`~.allowlist.WRITE_ACTIONS`; every
    other key is ignored here. ``infra`` (process-wide, part of no access
    decision) and ``profiles`` (which scopes whole rule sets to a browser profile)
    belong to the :class:`BrowdenRuntimeConfiguration` that owns this set; any
    other key has already been refused by the schema when the sections come from
    a loaded file.

    ``read_policy`` gates reads; ``denylist`` is the always-deny list (also
    consulted by write actions); ``section(name)`` and ``rules_for(name, ...)``
    gate write actions. An unlisted write action default-denies.
    """

    def __init__(self, sections: dict[str, object], tranco_path: Path | None = None,
                 *, base: "BrowdenAccessRuleSet | None" = None):
        # tranco_path is the Tranco snapshot that sits next to the allowlist
        # file; the loader/from_file pass it in. A bare dict construction (tests,
        # the import-time default) leaves it None -> the ~/.browden fallback.
        #
        # `base` is the set these rules sit ON TOP OF: a profile's block is
        # additive over the global one, so a profile starts from every global
        # rule and adds its own (see _merge_host_rules / _merge_read_cfg). None
        # for the global set itself. The merge happens on *coerced* rules, so
        # every input spelling — legacy host-wide mapping, bare path list, page
        # rule list — is already one shape by the time it is unioned.
        deny_rules = _coerce_section(sections.get("denylist"), want_label=False,
                                     where="denylist")
        read_cfg = sections.get("read")
        read_cfg = dict(read_cfg) if isinstance(read_cfg, dict) else {}
        override_rules = _coerce_section(read_cfg.get("website_overrides"), want_label=False,
                                         where="read.website_overrides")
        action_rules = {
            action: _coerce_section(sections.get(action), want_label=True, where=action)
            for action in WRITE_ACTIONS}
        allow_all = bool(sections.get("allow_all")) or (base is not None and base._allow_all)
        # Whether THIS set decided the popularity question for itself. Only an
        # explicit `tranco.enabled` here counts: under allow_all the net is
        # turned on rather than inherited, so a profile that wants the whole web
        # opts out in its own block instead of relying on a global setting.
        tranco_is_explicit = "enabled" in (read_cfg.get("tranco") or {})
        if base is not None:
            deny_rules = _merge_host_rules(base._deny_rules, deny_rules)
            read_cfg = _merge_read_cfg(base._read_cfg, read_cfg)
            override_rules = _merge_host_rules(base._override_rules, override_rules)
            action_rules = {
                action: _merge_host_rules(base._action_rules[action], action_rules[action])
                for action in WRITE_ACTIONS}
        if allow_all and not tranco_is_explicit:
            read_cfg["tranco"] = {**(read_cfg.get("tranco") or {}), "enabled": True}
        # Kept so a set built on top of this one can union against it.
        self._allow_all = allow_all
        self._deny_rules = deny_rules
        self._read_cfg = read_cfg
        self._override_rules = override_rules
        self._action_rules = action_rules

        # A denylist blocks broadly: prefix match, not fullmatch (see HostRuleMatcher).
        self._denylist = HostRuleMatcher.create_denylist(deny_rules)
        self._read_policy = self._build_read_policy(
            read_cfg, override_rules, self._denylist, tranco_path, allow_all=allow_all)
        # action -> canonical host -> ordered page rules (label + field_ids).
        self._rules: "dict[str, dict[str, list[PageRule]]]" = {
            action: {canonical_host(h): rs for h, rs in host_rules.items()}
            for action, host_rules in action_rules.items()}
        # section() keeps a page-admission view (labels ignored) for callers
        # that only ask "may this action touch this host+page at all".
        self._sections: dict[str, HostRuleMatcher] = {
            action: HostRuleMatcher.create_allowlist(host_rules)
            for action, host_rules in action_rules.items()}

    @staticmethod
    def _build_read_policy(read_cfg: dict, override_rules: "dict[str, list[PageRule]]",
                           denylist: HostRuleMatcher, tranco_path: Path | None,
                           *, allow_all: bool = False) -> ReadPolicy:
        """Assemble the ReadPolicy from the ``read`` block (fail-closed if absent).

        ``read_cfg`` carries the settings (``enabled`` / ``tranco``) and
        ``override_rules`` the already-coerced ``website_overrides``, which are
        kept apart so a profile's block can union its overrides with the global
        ones while merging the settings by key.

        With no ``read`` block the allowlist is enabled but empty, so only
        denylist + (nothing) applies — every read is denied until the operator
        opts sites in. The shipped sample enables Tranco so it works out of box.

        ``tranco_path`` is the snapshot sitting next to the allowlist config; if
        it is missing we fall back to the ~/.browden default (``path=None``), so
        a snapshot that setup has not fetched yet degrades to "Tranco matches
        nothing" rather than a crash.
        """
        enabled = bool(read_cfg.get("enabled", True))
        tranco_cfg = read_cfg.get("tranco") or {}
        tranco = None
        if tranco_cfg.get("enabled"):
            snapshot = tranco_path if (tranco_path and tranco_path.exists()) else None
            tranco = PopularityAllowlist(tranco_top_n=int(tranco_cfg.get("top_n", DEFAULT_TOP_N)), path=snapshot)
        return ReadPolicy(enabled=enabled, tranco=tranco,
                          overrides=HostRuleMatcher.create_allowlist(override_rules),
                          denylist=denylist, allow_all=allow_all)

    @property
    def read_policy(self) -> ReadPolicy:
        """The read/navigate gate (denylist + master switch + Tranco + overrides)."""
        return self._read_policy

    @property
    def denylist(self) -> HostRuleMatcher:
        """The always-deny list, so write actions can veto denied hosts too."""
        return self._denylist

    def is_denied(self, host: str, path: str) -> bool:
        """True if ``(host, path)`` is on the denylist (refused for every action)."""
        return self._denylist.is_allowed(host, path)

    def section(self, action: str) -> HostRuleMatcher:
        """Return the host/page-admission allowlist for ``action`` (labels ignored);
        an empty (deny-all) one if unlisted. Under ``allow_all``, every host."""
        if self._allow_all:
            return HostRuleMatcher.create_allowlist({"*": [_ANY_PAGE_ANY_CONTROL]})
        return self._sections.get(action) or HostRuleMatcher.create_allowlist({})

    def rules_for(self, action: str, host: str, path: str,
                  query: str = "", fragment: str = "") -> list[PageRule]:
        """The page rules for ``action`` on ``host`` that match this page, in order.

        Empty if the action/host is unlisted or no rule's page selector matches —
        the write gates read that as "this action is not authorized on this page"
        and refuse before touching the DOM. Never empty under ``allow_all``,
        which authorizes any control on any page of any host. Each returned rule
        carries the ``label`` and ``field_ids`` that authorize a control *on these
        pages*, so the caller checks the live element against the union of them.
        """
        by_host = self._rules.get(action) or {}
        rules = by_host.get(canonical_host(host)) or by_host.get("*") or []
        matched = [r for r in rules
                   if r.matches_page(path, query, fragment, full_match=True)]
        if self._allow_all:
            # Any control on any page, plus whatever was listed explicitly (which
            # can only be narrower, but costs nothing to keep).
            return [_ANY_PAGE_ANY_CONTROL, *matched]
        return matched
