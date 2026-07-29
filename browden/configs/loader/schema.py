"""Schema for the allowlist config file.

The expected shape (see configs/samples/read_only_on_popular_websites.yaml):

    denylist:                     # always refused; host -> path regexes
      <host>: [<path regex>, ...]

    read:                         # the read/navigate gate
      enabled: <bool>             # master switch (default true)
      tranco:
        enabled: <bool>           # allow the fetched top-sites snapshot
        top_n: <positive int>     # how far down the ranking to allow
      website_overrides:          # host -> path regexes; "*" host = any host
        <host>: [<path regex>, ...]

    <write action>:               # e.g. click
      <host>:
        label: <regex>               # REQUIRED: visible text must fully match
                                     #   (use '.*' to allow any control on the host)
        paths: [<path regex>, ...]   # optional, defaults to [".*"]

    infra:
      max_browser_sessions: <positive int>
      max_tabs_per_session: <positive int>
      reap_interval_seconds: <positive int>   # how often idle tabs are swept

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


def _check_path_selector(value, where: str) -> None:
    """A page selector: one regex string, or a non-empty list of them."""
    _check_patterns([value] if isinstance(value, str) else value, where)


def _check_label(rule, where: str) -> None:
    """A write action's required ``label`` regex."""
    if "label" not in rule:
        raise ConfigError(
            f"{where}: 'label' is required — a regex the control's visible text "
            f"must fully match (use '.*' to allow any control on this host)")
    label = rule["label"]
    if not isinstance(label, str):
        raise ConfigError(f"{where}.label: must be a regex string, got {type(label).__name__}")
    try:
        re.compile(label)
    except re.error as e:
        raise ConfigError(f"{where}.label: invalid regex {label!r}: {e}") from None


def _check_field_ids(fids, where: str) -> None:
    if not isinstance(fids, list) or not all(isinstance(x, str) and x for x in fids):
        raise ConfigError(f"{where}.field_ids: must be a list of non-empty id/name strings")


def _check_keys(keys, where: str) -> None:
    # A shape check only: the control-key allowlist (validator.ACTIVATION_KEYS) is
    # enforced at action time by the press-key gate, which refuses any non-control
    # key regardless of config — so a typo'd key here just never authorizes; it is
    # never a way to send a character key.
    if not isinstance(keys, list) or not keys or not all(isinstance(x, str) and x for x in keys):
        raise ConfigError(
            f"{where}.keys: must be a non-empty list of control-key names "
            f"(e.g. ['Enter', 'ArrowDown'])")


def _check_page_rule(rule, where: str, *, want_label: bool, allow_field_ids: bool,
                     allow_keys: bool = False) -> None:
    """Validate one page-rule mapping ``{path, match_on?, label?, field_ids?, keys?}``.

    ``want_label`` requires a ``label`` (write actions); when false a ``label`` is
    rejected as an unknown key (read overrides / denylist have none).
    ``field_ids`` is only accepted for ``write-text``; ``keys`` only for ``press-key``.
    """
    if not isinstance(rule, dict):
        raise ConfigError(f"{where}: each page rule must be a mapping, got {type(rule).__name__}")
    allowed = {"path", "match_on"}
    if want_label:
        allowed.add("label")
    if allow_field_ids:
        allowed.add("field_ids")
    if allow_keys:
        allowed.add("keys")
    unknown = set(rule) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)} (allowed: {', '.join(sorted(allowed))})")
    if "path" in rule:
        _check_path_selector(rule["path"], f"{where}.path")
    if "match_on" in rule and rule["match_on"] not in ("path", "url"):
        raise ConfigError(f"{where}.match_on: must be 'path' or 'url', got {rule['match_on']!r}")
    if want_label:
        _check_label(rule, where)
    if "field_ids" in rule:
        _check_field_ids(rule["field_ids"], where)
    if "keys" in rule:
        _check_keys(rule["keys"], where)


def _check_host_paths(rules, where: str) -> None:
    """Validate a host -> spec mapping (denylist / website_overrides).

    A host's spec is either the legacy list of path regexes ``["^/docs/.*"]`` or a
    list of label-less page rules ``[{path: ..., match_on: url}]`` (the page-rule
    form disambiguated by its elements being mappings, not strings).
    """
    if rules is None:
        return
    if not isinstance(rules, dict):
        raise ConfigError(f"{where}: expected a mapping of host -> path regexes, got {type(rules).__name__}")
    for host, spec in rules.items():
        # The empty host "" is permitted: it is the authority-less host that
        # file:// URLs carry (file:///etc/passwd), so `"": [<path regex>]` is how
        # an operator scopes which local-file paths are readable once file:// is
        # opted in. Every other host must be a non-empty string.
        if not isinstance(host, str):
            raise ConfigError(f"{where}: hosts must be strings, got {host!r}")
        if isinstance(spec, list) and spec and all(isinstance(x, dict) for x in spec):
            for i, rule in enumerate(spec):
                _check_page_rule(rule, f"{where}.{host}[{i}]", want_label=False, allow_field_ids=False)
        else:
            _check_patterns(spec, f"{where}.{host}")


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
                if name not in {"max_browser_sessions", "max_tabs_per_session",
                                "reap_interval_seconds"}:
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
        # `field_ids` (exact id/name allowlist for label-less text boxes) is only
        # meaningful for the write-text action; `keys` (the control keys a rule
        # authorizes) only for press-key. Other write actions may use neither.
        allow_field_ids = (action == "write-text")
        allow_keys = (action == "press-key")
        for host, rule in rules.items():
            where = f"{source}: {action}.{host}"
            if not isinstance(host, str) or not host:
                raise ConfigError(f"{source}: hosts in {action!r} must be non-empty strings, got {host!r}")
            # A host maps to a list of page rules (each a mapping with its own
            # label) OR the legacy single host-wide mapping. Both require a label
            # per rule — what a control may do is never the silent default of an
            # omission; "allow any control" reads as label: '.*'.
            if isinstance(rule, list):
                if not rule:
                    raise ConfigError(f"{where}: a write action needs at least one page rule")
                for i, page_rule in enumerate(rule):
                    _check_page_rule(page_rule, f"{where}[{i}]",
                                     want_label=True, allow_field_ids=allow_field_ids,
                                     allow_keys=allow_keys)
                continue
            if not isinstance(rule, dict):
                raise ConfigError(
                    f"{where}: rule must be a mapping with a required 'label' (and optional "
                    f"'paths'), or a list of page rules, got {type(rule).__name__}")
            allowed = ["paths", "label"] + (["field_ids"] if allow_field_ids else []) \
                + (["keys"] if allow_keys else [])
            unknown = set(rule) - set(allowed)
            if unknown:
                raise ConfigError(f"{where}: unknown keys {sorted(unknown)} (allowed: {', '.join(allowed)})")
            if "paths" in rule:
                _check_patterns(rule["paths"], f"{where}.paths")
            if "field_ids" in rule:
                _check_field_ids(rule["field_ids"], where)
            if "keys" in rule:
                _check_keys(rule["keys"], where)
            _check_label(rule, where)
    return data
