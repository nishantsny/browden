import json
import re
from pathlib import Path


def _canonical_host(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


class Allowlist:
    """Per-host path-regex allowlist. Use host key '*' for a wildcard fallback."""

    def __init__(self, rules: dict[str, list[str]]):
        self._rules: dict[str, list[re.Pattern[str]]] = {
            host.lower(): [re.compile(p) for p in patterns]
            for host, patterns in rules.items()
        }

    @classmethod
    def from_file(cls, path: Path) -> "Allowlist":
        return cls(json.loads(path.read_text()))

    def is_allowed(self, host: str, path: str) -> bool:
        patterns = self._rules.get(_canonical_host(host)) or self._rules.get("*")
        if not patterns:
            return False
        target = path or "/"
        return any(p.match(target) for p in patterns)


class ActionAllowlist:
    """Per-action host/path allowlist with optional per-host label requirements.

    Top-level keys are *action names*. A host's rules take one of two shapes:

    * **list form** — just path regexes (used by the read/navigate gate)::

          "read": {"*": [".*"]}

    * **object form** — path regexes plus a site-specific ``label`` regex the
      activated control's visible name must match (used by write actions)::

          "add_to_cart": {
            "amazon.com": {"paths": [".*"], "label": "(?i)\\\\badd to cart\\\\b"}
          }

    Reads stay wide-open; every write action carries its own explicit host list
    *and* the exact button text it expects on each site. ``section(name)`` gates
    host+path; ``label_pattern(name, host)`` returns the required label regex (or
    None). An unlisted action default-denies — a new write tool is inert until
    its hosts (and their labels) are listed here.
    """

    def __init__(self, sections: dict[str, dict[str, object]]):
        self._sections: dict[str, Allowlist] = {}
        self._labels: dict[str, dict[str, re.Pattern[str]]] = {}
        for action, rules in sections.items():
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

    @classmethod
    def from_file(cls, path: Path) -> "ActionAllowlist":
        return cls(json.loads(path.read_text()))

    def section(self, action: str) -> Allowlist:
        """Return the host/path allowlist for ``action``; an empty (deny-all) one if unlisted."""
        return self._sections.get(action) or Allowlist({})

    def label_pattern(self, action: str, host: str) -> "re.Pattern[str] | None":
        """Return the required visible-label regex for ``action`` on ``host``, or None if none configured."""
        return (self._labels.get(action) or {}).get(_canonical_host(host))
