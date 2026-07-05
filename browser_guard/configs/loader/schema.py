"""Schema for the allowlist config file.

The expected shape (see configs/samples/allowlist.yaml):

    <action name>:                # e.g. read, add_to_cart
      <host>:                     # e.g. amazon.com, or "*" as wildcard fallback
        - <path regex>            # list form: just path regexes (read-style)
      <host>:                     # or the object form used by write actions:
        paths: [<path regex>, ...]   # optional, defaults to [".*"]
        label: <regex>               # optional required visible-text regex

Validation is structural plus regex compilation; semantics (default-deny for
unlisted actions, www-stripping, ...) live in ActionAllowlist.
"""
import re


class ConfigError(ValueError):
    """A config file failed schema validation. Message names file and field."""


def _check_patterns(patterns, where: str) -> None:
    if not isinstance(patterns, list) or not patterns:
        raise ConfigError(f"{where}: expected a non-empty list of path regexes")
    for p in patterns:
        if not isinstance(p, str):
            raise ConfigError(f"{where}: path regex must be a string, got {type(p).__name__}")
        try:
            re.compile(p)
        except re.error as e:
            raise ConfigError(f"{where}: invalid path regex {p!r}: {e}") from None


def validate_allowlist_data(data, *, source: str = "allowlist") -> dict:
    """Validate parsed YAML against the allowlist schema; return it unchanged.

    Raises ConfigError with a message naming the offending field. An empty
    document is valid and means deny-all.
    """
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{source}: top level must be a mapping of action -> host rules, "
            f"got {type(data).__name__}")
    for action, rules in data.items():
        if not isinstance(action, str) or not action:
            raise ConfigError(f"{source}: action names must be non-empty strings, got {action!r}")
        if not isinstance(rules, dict):
            raise ConfigError(
                f"{source}: section {action!r} must be a mapping of host -> rule, "
                f"got {type(rules).__name__}")
        for host, rule in rules.items():
            where = f"{source}: {action}.{host}"
            if not isinstance(host, str) or not host:
                raise ConfigError(f"{source}: hosts in {action!r} must be non-empty strings, got {host!r}")
            if isinstance(rule, list):
                _check_patterns(rule, where)
            elif isinstance(rule, dict):
                unknown = set(rule) - {"paths", "label"}
                if unknown:
                    raise ConfigError(f"{where}: unknown keys {sorted(unknown)} (allowed: paths, label)")
                if "paths" in rule:
                    _check_patterns(rule["paths"], f"{where}.paths")
                label = rule.get("label")
                if label is not None:
                    if not isinstance(label, str):
                        raise ConfigError(f"{where}.label: must be a regex string, got {type(label).__name__}")
                    try:
                        re.compile(label)
                    except re.error as e:
                        raise ConfigError(f"{where}.label: invalid regex {label!r}: {e}") from None
            else:
                raise ConfigError(
                    f"{where}: rule must be a list of path regexes or a mapping "
                    f"with 'paths'/'label', got {type(rule).__name__}")
    return data
