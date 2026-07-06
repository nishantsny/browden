"""Schema for the allowlist config file.

The expected shape (see configs/samples/allowlist.yaml):

    denylist:                     # always refused; host -> path regexes
      <host>: [<path regex>, ...]

    read:                         # the read/navigate gate
      enabled: <bool>             # master switch (default true)
      tranco:
        enabled: <bool>           # allow the bundled top-sites snapshot
        top_n: <positive int>     # how far down the ranking to allow
      website_overrides:          # host -> path regexes; "*" host = any host
        <host>: [<path regex>, ...]

    <write action>:               # e.g. click
      <host>:
        - <path regex>            # list form: just path regexes
      <host>:                     # or object form with a required label:
        paths: [<path regex>, ...]   # optional, defaults to [".*"]
        label: <regex>               # optional required visible-text regex

    infra:
      max_browser_sessions: <positive int>
      max_tabs_per_session: <positive int>

Validation is structural plus regex compilation; semantics (default-deny,
denylist-wins ordering, www-stripping, ...) live in ActionAllowlist/ReadPolicy.
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


def _check_host_paths(rules, where: str) -> None:
    """Validate a host -> [path regex] mapping (denylist / website_overrides)."""
    if rules is None:
        return
    if not isinstance(rules, dict):
        raise ConfigError(f"{where}: expected a mapping of host -> path regexes, got {type(rules).__name__}")
    for host, patterns in rules.items():
        if not isinstance(host, str) or not host:
            raise ConfigError(f"{where}: hosts must be non-empty strings, got {host!r}")
        _check_patterns(patterns, f"{where}.{host}")


def _check_read(rules, where: str) -> None:
    if not isinstance(rules, dict):
        raise ConfigError(f"{where}: must be a mapping, got {type(rules).__name__}")
    unknown = set(rules) - {"enabled", "tranco", "website_overrides"}
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)} (allowed: enabled, tranco, website_overrides)")
    if "enabled" in rules and not isinstance(rules["enabled"], bool):
        raise ConfigError(f"{where}.enabled: must be a boolean, got {type(rules['enabled']).__name__}")
    tranco = rules.get("tranco")
    if tranco is not None:
        if not isinstance(tranco, dict):
            raise ConfigError(f"{where}.tranco: must be a mapping, got {type(tranco).__name__}")
        unknown = set(tranco) - {"enabled", "top_n"}
        if unknown:
            raise ConfigError(f"{where}.tranco: unknown keys {sorted(unknown)} (allowed: enabled, top_n)")
        if "enabled" in tranco and not isinstance(tranco["enabled"], bool):
            raise ConfigError(f"{where}.tranco.enabled: must be a boolean, got {type(tranco['enabled']).__name__}")
        top_n = tranco.get("top_n")
        # bool is an int subclass — reject it explicitly so `top_n: true` fails.
        if top_n is not None and (not isinstance(top_n, int) or isinstance(top_n, bool) or top_n <= 0):
            raise ConfigError(f"{where}.tranco.top_n: must be a positive integer, got {top_n!r}")
    _check_host_paths(rules.get("website_overrides"), f"{where}.website_overrides")


def validate_allowlist_data(data, *, source: str = "allowlist") -> dict:
    """Validate parsed YAML against the allowlist schema; return it unchanged.

    Raises ConfigError with a message naming the offending field. An empty
    document is valid and means deny-all reads with no write actions.
    """
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{source}: top level must be a mapping, got {type(data).__name__}")
    for key, rules in data.items():
        if key == "infra":
            if not isinstance(rules, dict):
                raise ConfigError(f"{source}: infra must be a mapping, got {type(rules).__name__}")
            for name, value in rules.items():
                if name not in {"max_browser_sessions", "max_tabs_per_session"}:
                    raise ConfigError(f"{source}: unknown infra key {name}")
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise ConfigError(f"{source}: infra.{name} must be a positive integer, got {value}")
            continue
        if key == "denylist":
            _check_host_paths(rules, f"{source}: denylist")
            continue
        if key == "read":
            _check_read(rules, f"{source}: read")
            continue

        # Everything else is a write action: host -> (path regexes | object form).
        action = key
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
