"""Load and resolve allowlist config files.

``load_allowlist`` is the one path from a YAML file to the internal
``ActionAllowlist`` structure: read, parse, schema-validate, convert. The MCP
server's ``main()`` calls it with the resolved path and hands the result to
the tool layer.
"""
import os
from pathlib import Path

import yaml

from ...mcp.validator.allowlist import ActionAllowlist
from .schema import ConfigError, validate_allowlist_data

# loader/ -> configs/ -> browser_guard/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_ALLOWLIST = _REPO_ROOT / "configs" / "samples" / "allowlist.yaml"
USER_CONFIG_DIR = Path("~/.browser_guard")


def load_allowlist(path: Path | str) -> ActionAllowlist:
    """Read ``path``, verify it against the schema, and build the allowlist."""
    path = Path(path).expanduser()
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ConfigError(f"allowlist config not found: {path}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    return ActionAllowlist(validate_allowlist_data(data, source=str(path)))


def resolve_allowlist_path(explicit: str | None = None) -> Path | None:
    """Pick the allowlist file to load, most specific first.

    Explicit CLI argument > ``BROWSER_GUARD_ALLOWLIST`` env var >
    ``~/.browser_guard/allowlist.yaml`` (installed by setup/onetime_setup.py) >
    the repo sample. None if nothing is found.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("BROWSER_GUARD_ALLOWLIST")
    if env:
        return Path(env).expanduser()
    user = (USER_CONFIG_DIR / "allowlist.yaml").expanduser()
    if user.exists():
        return user
    if SAMPLE_ALLOWLIST.exists():
        return SAMPLE_ALLOWLIST
    return None
