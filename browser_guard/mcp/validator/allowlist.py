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
