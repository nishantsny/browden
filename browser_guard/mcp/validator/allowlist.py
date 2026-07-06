import re
from pathlib import Path

import yaml

from .tranco import BUNDLED_TOP_N, TrancoList


def _canonical_host(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _paths_map(rules: object) -> dict[str, list[str]]:
    """Coerce a host -> (list | {'paths': ...}) mapping to host -> [path regex]."""
    out: dict[str, list[str]] = {}
    for host, spec in (rules or {}).items():
        out[host] = spec.get("paths", [".*"]) if isinstance(spec, dict) else spec
    return out


class Allowlist:
    """Per-host path-regex allowlist. Use host key '*' for a wildcard fallback."""

    def __init__(self, rules: dict[str, list[str]]):
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
        target = path or "/"
        return any(p.match(target) for p in patterns)

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
       bundled top-sites snapshot.

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
            tranco: {enabled: true, top_n: 100000}
            website_overrides:
              "*": [".*"]                 # host -> path regexes; "*" = any host

    * any other key (e.g. ``click``) — a *write action*, whose per-host rules
      take a list of path regexes or the object form with a required ``label``
      regex the activated control's visible name must match::

          click:
            amazon.com:
              paths: [".*"]
              label: '(?i)\\badd to cart\\b'
    * ``infra`` — session/tab caps.

    ``read_policy`` gates reads; ``denylist`` is the always-deny list (also
    consulted by write actions); ``section(name)``/``label_pattern(name, host)``
    gate write actions. An unlisted write action default-denies.
    """

    def __init__(self, sections: dict[str, object]):
        self._sections: dict[str, Allowlist] = {}
        self._labels: dict[str, dict[str, re.Pattern[str]]] = {}
        self.max_browser_sessions = 10
        self.max_tabs_per_session = 20
        self._denylist = Allowlist(_paths_map(sections.get("denylist")))
        self._read_policy = self._build_read_policy(sections.get("read"), self._denylist)
        for action, rules in sections.items():
            if action in ("infra", "read", "denylist"):
                if action == "infra" and isinstance(rules, dict):
                    self.max_browser_sessions = int(rules.get("max_browser_sessions", 10))
                    self.max_tabs_per_session = int(rules.get("max_tabs_per_session", 20))
                continue
            paths: dict[str, list[str]] = {}
            labels: dict[str, re.Pattern[str]] = {}
            for host, spec in rules.items():
                if isinstance(spec, dict):
                    paths[host] = spec.get("paths", [".*"])
                    label = spec.get("label")
                    if label:
                        labels[_canonical_host(host)] = re.compile(label)
                else:  # list of path regexes (read-style)
                    paths[host] = spec
            self._sections[action] = Allowlist(paths)
            self._labels[action] = labels

    @staticmethod
    def _build_read_policy(read_cfg: object, denylist: Allowlist) -> ReadPolicy:
        """Assemble the ReadPolicy from the ``read`` block (fail-closed if absent).

        With no ``read`` block the allowlist is enabled but empty, so only
        denylist + (nothing) applies — every read is denied until the operator
        opts sites in. The shipped sample enables Tranco so it works out of box.
        """
        cfg = read_cfg if isinstance(read_cfg, dict) else {}
        enabled = bool(cfg.get("enabled", True))
        tranco_cfg = cfg.get("tranco") or {}
        tranco = None
        if tranco_cfg.get("enabled"):
            tranco = TrancoList(top_n=int(tranco_cfg.get("top_n", BUNDLED_TOP_N)))
        overrides = Allowlist(_paths_map(cfg.get("website_overrides")))
        return ReadPolicy(enabled=enabled, tranco=tranco, overrides=overrides, denylist=denylist)

    @classmethod
    def from_file(cls, path: Path) -> "ActionAllowlist":
        return cls(yaml.safe_load(path.read_text()) or {})

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
