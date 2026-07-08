import re
from pathlib import Path

import yaml

from .tranco import DEFAULT_TOP_N, TRANCO_FILENAME, TrancoList


def _canonical_host(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


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


def _paths_map(rules: object) -> dict[str, list[str]]:
    """Coerce a host -> (list | {'paths': ...}) mapping to host -> [path regex]."""
    out: dict[str, list[str]] = {}
    for host, spec in (rules or {}).items():
        out[host] = spec.get("paths", [".*"]) if isinstance(spec, dict) else spec
    return out


class Allowlist:
    """Per-host path-regex allowlist. Use host key '*' for a wildcard fallback."""

    def __init__(self, rules: dict[str, list[str]], *, full_match: bool = True):
        # full_match decides how a path regex is applied. An *allow* list
        # fullmatches (the pattern must span the whole path) so `^/products` does
        # not also wave through `/products-secret-admin`. A *denylist* is the
        # opposite risk — it should block broadly — so it keeps prefix semantics
        # (start-anchored `re.match`): `^/checkout` still denies `/checkout/pay`.
        self._full_match = full_match
        self._rules: dict[str, list[re.Pattern[str]]] = {
            host.lower(): [re.compile(p) for p in patterns]
            for host, patterns in rules.items()
        }

    @classmethod
    def from_file(cls, path: Path) -> "Allowlist":
        return cls(yaml.safe_load(path.read_text()) or {})

    def is_allowed(self, host: str, path: str) -> bool:
        patterns = self._rules.get(_canonical_host(host)) or self._rules.get("*")
        if not patterns:
            return False
        target = _normalize_path(path or "/")
        # An allow list fullmatches (see __init__): `^/products` must cover the
        # whole path, so it does not also permit `/products-secret-admin`; a
        # prefix rule is spelled `^/products/.*`. The denylist keeps prefix match
        # so it still blocks broadly.
        return any(
            (p.fullmatch(target) if self._full_match else p.match(target))
            for p in patterns
        )

    def covers(self, host: str) -> bool:
        """True if a rule set governs ``host`` (an exact entry or the ``*`` wildcard).

        Distinct from ``is_allowed``: a host can be *covered* (has rules) yet be
        denied because its path doesn't match. Lets the read policy give an
        explicit override precedence over Tranco even when it path-scopes a host.
        """
        return _canonical_host(host) in self._rules or "*" in self._rules


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

    def __init__(self, *, enabled: bool, tranco: TrancoList | None,
                 overrides: Allowlist, denylist: Allowlist):
        self._enabled = enabled
        self._tranco = tranco
        self._overrides = overrides
        self._denylist = denylist

    def is_allowed(self, host: str, path: str) -> bool:
        if self._denylist.is_allowed(host, path):
            return False
        if not self._enabled:
            return True
        # An explicit override for this host wins over Tranco — even to restrict
        # (path-scope) a host Tranco would otherwise allow wholesale.
        if self._overrides.covers(host):
            return self._overrides.is_allowed(host, path)
        return self._tranco is not None and self._tranco.contains(host)


class ActionAllowlist:
    """The whole access policy: a denylist, the read allowlist, and write actions.

    Top-level keys are handled as follows:

    * ``denylist`` — host -> path regexes that are **always refused** (checked
      first, wins over everything). ``*`` host matches any host.
    * ``read`` — the read/navigate gate, built into a :class:`ReadPolicy`::

          read:
            enabled: true                 # master switch for the read allowlist
            tranco: {enabled: true, top_n: 1000000}
            website_overrides:
              "*": [".*"]                 # host -> path regexes; "*" = any host

    * any other key (e.g. ``click``) — a *write action*, whose per-host rule is
      a mapping with a **required** ``label`` regex (the activated control's
      visible name must fully match it) and optional ``paths``::

          click:
            amazon.com:
              label: '(?i)add to cart'   # required; use '.*' to allow any control
              paths: [".*"]              # optional, defaults to [".*"]

      The label is mandatory so that allowing every control reads explicitly as
      ``label: '.*'`` in the config, never as the silent default of an omission.
    * ``write-text`` — the text-entry write action (the ``insert_text`` tool), a section
      *separate* from ``click`` so permitting typing never implies permitting
      clicks. Same shape (per-host ``label`` regex + optional ``paths``), but the
      label is matched against the **field's visible label** — its placeholder,
      aria-label, resolved ``aria-labelledby`` / ``<label>``, or title — so an
      operator authorizes *which* text boxes may be typed into by the name a human
      reads next to them::

          write-text:
            amazon.com:
              label: '(?i)grocery tip.*'  # only fields labelled "Grocery Tip …"
              paths: [".*"]
              field_ids:                  # optional escape hatch (see below)
                - tip-widget--edit-form--amount-input

      A ``write-text`` host may additionally list ``field_ids``: exact ``id`` /
      ``name`` values that authorize a text box carrying **no visible label**
      (e.g. Amazon's Fresh grocery-tip input), which the label regex can never
      match. This trusts a non-visible identifier, so it is opt-in per field and
      never implied — omit it and only visibly-labelled fields are typable.
    * ``infra`` — session/tab caps.

    ``read_policy`` gates reads; ``denylist`` is the always-deny list (also
    consulted by write actions); ``section(name)``/``label_pattern(name, host)``
    gate write actions. An unlisted write action default-denies.
    """

    def __init__(self, sections: dict[str, object], tranco_path: Path | None = None):
        # tranco_path is the Tranco snapshot that sits next to the allowlist
        # file; the loader/from_file pass it in. A bare dict construction (tests,
        # the import-time default) leaves it None -> the ~/.browden fallback.
        self._sections: dict[str, Allowlist] = {}
        self._labels: dict[str, dict[str, re.Pattern[str]]] = {}
        self._field_ids: dict[str, dict[str, set[str]]] = {}
        self.max_browser_sessions = 10
        self.max_tabs_per_session = 20
        # A denylist blocks broadly: prefix match, not fullmatch (see Allowlist).
        self._denylist = Allowlist(_paths_map(sections.get("denylist")), full_match=False)
        self._read_policy = self._build_read_policy(
            sections.get("read"), self._denylist, tranco_path)
        for action, rules in sections.items():
            if action in ("infra", "read", "denylist"):
                if action == "infra" and isinstance(rules, dict):
                    self.max_browser_sessions = int(rules.get("max_browser_sessions", 10))
                    self.max_tabs_per_session = int(rules.get("max_tabs_per_session", 20))
                continue
            paths: dict[str, list[str]] = {}
            labels: dict[str, re.Pattern[str]] = {}
            field_ids: dict[str, set[str]] = {}
            for host, spec in rules.items():
                # A write-action host must declare a label — what a control may
                # do is never implicit. "Allow any control" is spelled '.*'.
                if not isinstance(spec, dict) or "label" not in spec:
                    raise ValueError(
                        f"{action}.{host}: a write-action host requires a 'label' regex "
                        f"(use '.*' to allow any control on this host)")
                paths[host] = spec.get("paths", [".*"])
                labels[_canonical_host(host)] = re.compile(spec["label"])
                # Optional: exact field id/name values that authorize a text box
                # with no visible label (write-text only; see field_id_matches).
                raw_ids = spec.get("field_ids") or []
                if raw_ids and not isinstance(raw_ids, list):
                    raise ValueError(
                        f"{action}.{host}: 'field_ids' must be a list of id/name strings")
                field_ids[_canonical_host(host)] = {str(x) for x in raw_ids}
            self._sections[action] = Allowlist(paths)
            self._labels[action] = labels
            self._field_ids[action] = field_ids

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
            tranco = TrancoList(top_n=int(tranco_cfg.get("top_n", DEFAULT_TOP_N)), path=snapshot)
        overrides = Allowlist(_paths_map(cfg.get("website_overrides")))
        return ReadPolicy(enabled=enabled, tranco=tranco, overrides=overrides, denylist=denylist)

    @classmethod
    def from_file(cls, path: Path) -> "ActionAllowlist":
        return cls(yaml.safe_load(path.read_text()) or {},
                   tranco_path=path.parent / TRANCO_FILENAME)

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
        """Return the host/path allowlist for ``action``; an empty (deny-all) one if unlisted."""
        return self._sections.get(action) or Allowlist({})

    def label_pattern(self, action: str, host: str) -> "re.Pattern[str] | None":
        """Return the required visible-label regex for ``action`` on ``host``, or None if none configured."""
        return (self._labels.get(action) or {}).get(_canonical_host(host))

    def field_ids(self, action: str, host: str) -> "set[str]":
        """The exact field id/name values authorized for ``action`` on ``host`` (empty set if none).

        Only meaningful for ``write-text``: these name label-less text boxes that
        the visible-label regex cannot reach (see ``intent.field_id_matches``).
        """
        return (self._field_ids.get(action) or {}).get(_canonical_host(host), set())
