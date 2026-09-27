"""The access rules a request is decided by: denylist, read gate, write actions.

:class:`BrowdenAccessRuleSet` is the evaluated rule surface every gate consults.
It is built from the rule sections of a loaded config and holds nothing
process-wide — the ``infra`` caps live on
:class:`~.runtime_configuration.BrowdenRuntimeConfiguration`, which owns one of these.
"""
from pathlib import Path

from .allowlist import WRITE_ACTIONS, Allowlist, PageRule, ReadPolicy, _coerce_section
from .popularity import PopularityAllowlist
from .tranco import DEFAULT_TOP_N, canonical_host


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
    Write actions are read by name from :data:`~.allowlist.WRITE_ACTIONS`; every
    other key is ignored here. ``infra`` is process-wide, not part of any access
    decision, and belongs to the :class:`BrowdenRuntimeConfiguration` that owns
    this set; any other key has already been refused by the schema when the
    sections come from a loaded file.

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
        for action in WRITE_ACTIONS:
            host_rules = _coerce_section(sections.get(action), want_label=True, where=action)
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
