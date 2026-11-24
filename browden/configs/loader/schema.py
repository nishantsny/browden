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

    <write action>:               # click | write-text | press-key | upload-file
      <host>:
        label: <regex>               # REQUIRED: visible text must fully match
                                     #   (use '.*' to allow any control on the host)
        paths: [<path regex>, ...]   # optional, defaults to [".*"]
        field_ids: [<id or name>]    # write-text / upload-file: label-less controls
        hidden_input: <bool>         # upload-file only: field_ids / accept also admit a
                                     #   file input the page hid behind its own button
        accept: [<accept value>]     # upload-file only: a file input with no id/name,
                                     #   named by its exact `accept` attribute (whitespace
                                     #   around commas ignored; no regex; no '*' / '*/*');
                                     #   it must be the page's only such input with that value
        max_bytes: <positive int>    # upload-file only: the cap on controls this rule
                                     #   admits, in place of max_upload_bytes (no ceiling;
                                     #   the largest cap among admitting rules wins)

    allowed_upload_locations:                 # the only directories `upload-file` may read
      - <path>                    #   from; absent/empty => every upload denied

    max_upload_bytes: <positive int>  # size cap for an upload-file rule that sets no
                                      #   max_bytes; absent => 1048576 (1 MiB)

    profiles:                     # per-browser-profile rule sets (additive over
      <profile dir>:              #   the global ones above)
        description: <string>     # what this profile is for; list_profiles returns it verbatim
        allow_all: <bool>         # every action in THIS profile; Tranco stays on
        <any of the sections above except infra/profiles/max_upload_bytes>

    infra:
      max_browser_sessions: <positive int>
      max_tabs_per_session: <positive int>
      reap_interval_seconds: <positive int>   # how often idle tabs are swept
      auto_restart_on_chrome_update: <bool>   # each sweep restarts a Chrome whose installed
                                              #   version changed (default true)

Any other top-level key is refused: an unknown section would be kept as rules
no gate ever consults, so a typo like `clik:` would silently authorize nothing.

Validation is structural plus regex compilation; semantics (default-deny,
denylist-wins ordering, www-stripping, ...) live in BrowdenAccessRuleSet and
ReadPolicy.
"""
import re
from pathlib import Path

from ...mcp.validator.allowlist import WRITE_ACTIONS

# Top-level sections that are not write actions, each checked by its own branch.
_OTHER_SECTIONS = ("denylist", "read", "infra", "profiles", "allowed_upload_locations",
                   "max_upload_bytes")


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


# Wildcards that name no type at all: an input carrying one takes any file, so
# naming it by that value says nothing about which control it is.
_ACCEPT_ANY = frozenset({"*", "*/*"})


def _check_accept(values, where: str) -> None:
    # Exact attribute values, never regexes: a pattern here would let one rule
    # name every file input on the page.
    if not isinstance(values, list) or not values or not all(isinstance(x, str) and x.strip()
                                                             for x in values):
        raise ConfigError(
            f"{where}.accept: must be a non-empty list of non-empty accept values "
            f"(e.g. ['video/*'])")
    for value in values:
        parts = [p.strip() for p in value.split(",")]
        if not all(parts):
            raise ConfigError(
                f"{where}.accept: {value!r} has an empty comma-separated part")
        if any(p in _ACCEPT_ANY for p in parts):
            raise ConfigError(
                f"{where}.accept: {value!r} accepts any file ('*' / '*/*'), so it names "
                f"no particular input")


def _check_hidden_input(rule: dict, where: str) -> None:
    # The opt-in admits a hidden file input only by what the rule names — its
    # id/name (field_ids) or its exact accept value (accept) — so it is
    # meaningless without one of them: refuse it rather than load a rule the
    # operator believes is doing something.
    if not isinstance(rule["hidden_input"], bool):
        raise ConfigError(f"{where}.hidden_input: must be true or false")
    if rule["hidden_input"] and not (rule.get("field_ids") or rule.get("accept")):
        raise ConfigError(
            f"{where}.hidden_input: needs field_ids or accept — a hidden file input is "
            f"admitted only by the exact id/name or accept value the rule names")


def _check_positive_int(value, where: str) -> None:
    # bool is an int subclass — reject it explicitly so `max_bytes: true` fails.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{where}: must be a positive integer (bytes), got {value!r}")


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
                     allow_keys: bool = False, allow_hidden_input: bool = False) -> None:
    """Validate one page-rule mapping ``{path, match_on?, label?, field_ids?, keys?, hidden_input?, accept?, max_bytes?}``.

    ``want_label`` requires a ``label`` (write actions); when false a ``label`` is
    rejected as an unknown key (read overrides / denylist have none).
    ``field_ids`` is accepted for ``write-text`` and ``upload-file``; ``keys`` only
    for ``press-key``; ``hidden_input``, ``accept`` and ``max_bytes`` only for
    ``upload-file`` (``allow_hidden_input`` admits all three).
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
    if allow_hidden_input:
        allowed.update(("hidden_input", "accept", "max_bytes"))
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
    if "accept" in rule:
        _check_accept(rule["accept"], where)
    if "hidden_input" in rule:
        _check_hidden_input(rule, where)
    if "max_bytes" in rule:
        _check_positive_int(rule["max_bytes"], f"{where}.max_bytes")


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


def _check_infra(rules, source: str) -> None:
    """Validate the ``infra`` block: the process-wide session/tab caps and housekeeping."""
    if not isinstance(rules, dict):
        raise ConfigError(f"{source}: infra must be a mapping, got {type(rules).__name__}")
    for name, value in rules.items():
        if name == "auto_restart_on_chrome_update":
            if not isinstance(value, bool):
                raise ConfigError(f"{source}: infra.{name} must be a boolean, got {value!r}")
            continue
        if name not in {"max_browser_sessions", "max_tabs_per_session",
                        "reap_interval_seconds"}:
            raise ConfigError(f"{source}: unknown infra key {name}")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{source}: infra.{name} must be a positive integer, got {value}")


def _check_upload_locations(locations, source: str) -> None:
    """Validate ``allowed_upload_locations``: the directories ``upload-file`` may read from.

    Each location must be absolute once ``~`` is expanded. A relative one would
    resolve against the server's working directory, so the same config would
    bound uploads to a different directory depending on how the server was
    launched — the identical reason a ``profiles`` key must be absolute. It fails
    the load with its own spelling rather than sitting inert in a config the
    operator believes is in force.
    """
    if not isinstance(locations, list) or not all(isinstance(r, str) and r.strip() for r in locations):
        raise ConfigError(
            f"{source}: allowed_upload_locations must be a list of directory paths "
            f"(an empty list denies every upload)")
    for location in locations:
        try:
            expanded = Path(location).expanduser()
        except (OSError, RuntimeError, ValueError) as e:
            raise ConfigError(
                f"{source}: allowed_upload_locations entry {location!r}: "
                f"not a usable path: {e}") from None
        if not expanded.is_absolute():
            raise ConfigError(
                f"{source}: allowed_upload_locations entry {location!r}: must be an absolute directory "
                f"(a relative path would depend on the server's working directory)")


def _check_write_action(action, rules, source: str) -> None:
    """Validate one write-action section: host -> (page-rule list | legacy mapping).

    ``action`` is one of :data:`WRITE_ACTIONS`; :func:`_check_section` has
    already refused any other key.
    """
    if not isinstance(rules, dict):
        raise ConfigError(
            f"{source}: section {action!r} must be a mapping of host -> rule, "
            f"got {type(rules).__name__}")
    # `field_ids` (an exact id/name allowlist for a control carrying no visible
    # label) is meaningful for write-text and upload-file — the two actions that
    # target form fields, which often have no label a regex could match. A file
    # input usually has none at all, so for upload-file it is the normal handle
    # rather than an escape hatch. `keys` (the control keys a rule authorizes) is
    # press-key's alone; `click` takes neither. `hidden_input` (admit a file input
    # the page hid behind its own picker button), `accept` (name a file input with
    # no id/name by its exact accept value) and `max_bytes` (this rule's own
    # upload size cap) are upload-file's alone.
    allow_field_ids = action in ("write-text", "upload-file")
    allow_keys = (action == "press-key")
    allow_hidden_input = (action == "upload-file")
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
                                 allow_keys=allow_keys, allow_hidden_input=allow_hidden_input)
            continue
        if not isinstance(rule, dict):
            raise ConfigError(
                f"{where}: rule must be a mapping with a required 'label' (and optional "
                f"'paths'), or a list of page rules, got {type(rule).__name__}")
        allowed = ["paths", "label"] + (["field_ids"] if allow_field_ids else []) \
            + (["keys"] if allow_keys else []) \
            + (["hidden_input", "accept", "max_bytes"] if allow_hidden_input else [])
        unknown = set(rule) - set(allowed)
        if unknown:
            raise ConfigError(f"{where}: unknown keys {sorted(unknown)} (allowed: {', '.join(allowed)})")
        if "paths" in rule:
            _check_patterns(rule["paths"], f"{where}.paths")
        if "field_ids" in rule:
            _check_field_ids(rule["field_ids"], where)
        if "keys" in rule:
            _check_keys(rule["keys"], where)
        if "accept" in rule:
            _check_accept(rule["accept"], where)
        if "hidden_input" in rule:
            _check_hidden_input(rule, where)
        if "max_bytes" in rule:
            _check_positive_int(rule["max_bytes"], f"{where}.max_bytes")
        _check_label(rule, where)


def _check_profiles(rules, source: str) -> None:
    """Validate the ``profiles`` block: profile directory -> its own rule set.

    Each key names a browser profile (Chrome's ``--user-data-dir``) and must be
    an absolute path (``~`` allowed) — a relative one would resolve against the
    server's working directory, so the same config would scope rules to a
    different profile depending on how the server was launched. It fails the load
    with its own spelling in the message rather than sitting inert in a config the
    operator believes is in force.

    The body is validated by the *same* :func:`_check_section` as a top-level
    one, minus the keys that make no sense inside a profile: ``infra`` and
    ``max_upload_bytes`` are process-wide (one Chrome cap can't differ per
    profile; a profile that needs bigger uploads sets ``max_bytes`` on its own
    ``upload-file`` rules) and ``profiles`` doesn't nest. Both are rejected rather than ignored — a cap written where it
    has no effect is a misunderstanding worth surfacing at load.
    """
    if not isinstance(rules, dict):
        raise ConfigError(
            f"{source}: profiles must be a mapping of profile dir -> rules, "
            f"got {type(rules).__name__}")
    for profile_dir, body in rules.items():
        if not isinstance(profile_dir, str) or not profile_dir.strip():
            raise ConfigError(f"{source}: profile keys must be non-empty paths, got {profile_dir!r}")
        try:
            expanded = Path(profile_dir).expanduser()
        except (OSError, RuntimeError, ValueError) as e:
            raise ConfigError(f"{source}: profiles.{profile_dir}: not a usable path: {e}") from None
        # Checked *before* resolving: `resolve()` would silently anchor a relative
        # key to whatever directory the server happens to be started from, so the
        # same config would scope rules to a different profile depending on how it
        # was launched. A profile is named absolutely (`~` is fine) or not at all.
        if not expanded.is_absolute():
            raise ConfigError(
                f"{source}: profiles.{profile_dir!r}: must be an absolute profile "
                f"directory (a relative path would depend on the server's working "
                f"directory)")
        where = f"{source}: profiles.{profile_dir}"
        if body is None:
            continue
        if not isinstance(body, dict):
            raise ConfigError(f"{where}: must be a mapping of rules, got {type(body).__name__}")
        for key, section in body.items():
            if key in ("infra", "profiles", "max_upload_bytes"):
                raise ConfigError(
                    f"{where}: {key!r} is not allowed inside a profile — it is "
                    f"process-wide; move it to the top level")
            if key == "description":
                if not isinstance(section, str) or not section.strip():
                    raise ConfigError(
                        f"{where}.description: must be a non-empty string, got {section!r}")
                continue
            if key == "allow_all":
                if not isinstance(section, bool):
                    raise ConfigError(
                        f"{where}.allow_all: must be a boolean, got {type(section).__name__}")
                continue
            _check_section(key, section, source=where)


def _check_section(key, rules, *, source: str) -> None:
    """Validate one top-level section of a config document.

    The named sections first, then the write actions; any other key is refused
    rather than kept as rules no gate ever consults.
    """
    if key == "infra":
        _check_infra(rules, source)
        return
    if key == "denylist":
        _check_host_paths(rules, f"{source}: denylist")
        return
    if key == "read":
        _check_read(rules, f"{source}: read")
        return
    if key == "profiles":
        _check_profiles(rules, source)
        return
    if key == "allowed_upload_locations":
        _check_upload_locations(rules, source)
        return
    if key == "max_upload_bytes":
        _check_positive_int(rules, f"{source}: max_upload_bytes")
        return
    if key == "allow_all":
        # Only meaningful inside a profile: "allow everything" is a thing you say
        # about ONE browsing identity, never about every profile the server drives
        # — which is what it would mean here. Caught by name so the message says
        # that, rather than the generic "unknown section" a typo would get.
        raise ConfigError(
            f"{source}: allow_all is only allowed inside a profiles entry — "
            f"it opens every action in one profile, not across all of them")
    if key not in WRITE_ACTIONS:
        raise ConfigError(
            f"{source}: unknown section {key!r} "
            f"(allowed: {', '.join(sorted((*_OTHER_SECTIONS, *WRITE_ACTIONS)))})")
    _check_write_action(key, rules, source)


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
        _check_section(key, rules, source=source)
    return data
