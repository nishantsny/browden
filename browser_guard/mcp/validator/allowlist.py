import json
import re
from pathlib import Path

_ALLOWLIST_PATH = Path(__file__).parent / "allowlist.json"


def _load() -> dict[str, list[re.Pattern[str]]]:
    raw = json.loads(_ALLOWLIST_PATH.read_text())
    return {host.lower(): [re.compile(p) for p in patterns] for host, patterns in raw.items()}


_RULES = _load()


def _canonical_host(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_allowed(host: str, path: str) -> bool:
    """True if (host, path) matches any allowlist regex for that host."""
    patterns = _RULES.get(_canonical_host(host))
    if not patterns:
        return False
    target = path or "/"
    return any(p.match(target) for p in patterns)
