"""The runtime configuration browden runs under: one loaded allowlist file.

:class:`BrowdenRuntimeConfiguration` is what the loader builds from the allowlist
YAML and the refresher hot-swaps: the process-wide ``infra`` caps plus the
:class:`~.access_rule_set.BrowdenAccessRuleSet` every gate decides against.
"""
from pathlib import Path

import yaml

from ...common.profile import canonical_profile_dir
from .access_rule_set import BrowdenAccessRuleSet
from .tranco import TRANCO_FILENAME

# Defaults for the `infra` section. They live here, with the rest of the config
# layer, so there is exactly one place that says what an unset knob means; the
# session layer imports the reap default rather than restating it.
DEFAULT_MAX_BROWSER_SESSIONS = 10
DEFAULT_MAX_TABS_PER_SESSION = 20
# How often the idle reaper wakes. Deliberately coarse: sweeping is the only
# cleanup pass (tools never sweep), and a tab idle for an hour can wait.
DEFAULT_REAP_INTERVAL_SECONDS = 7200


class BrowdenRuntimeConfiguration:
    """A whole loaded config: the process-wide ``infra`` caps plus the access rules.

    Two kinds of setting live in an allowlist file and they behave differently,
    so they are held apart here:

    * ``infra`` — ``max_browser_sessions`` / ``max_tabs_per_session`` /
      ``reap_interval_seconds``. Process-wide resource caps, read by the session
      layer; they gate no access decision and belong to no single request.
    * everything else — ``denylist``, ``read``, and the write actions: the rules
      that decide whether a given request may act. They are parsed into a
      :class:`BrowdenAccessRuleSet` (which documents the grammar), reachable as
      :attr:`access_rules`.

    ``profiles`` scopes a *second* copy of that same rule grammar to one browser
    profile — the directory Chrome runs under, which is what actually separates
    one browsing identity (its cookies, extensions, logins) from another::

        denylist:                        # global: every profile gets these
          "*": ['^/admin/.*']
        read:
          tranco: {enabled: true, top_n: 1000000}

        profiles:
          ~/.cache/browden/chrome-profile:      # the credentialed profile
            click:
              secure.splitwise.com:
                label: 'Save'
                paths: ['^/expenses.*']

    A profile's rules are **additive over the global ones**: its set starts from
    every global rule and adds its own, so widening one profile never narrows
    another and a profile with no block gets exactly the global rules (which is
    every config written before this existed). The denylist is unioned the same
    way and still wins over everything, global entry or profile entry.

    Keys are canonicalized with :func:`canonical_profile_dir` — the same
    reduction the session layer applies to a caller's ``profile_dir`` — so a key
    written ``~/.cache/browden/p`` names the same profile as the resolved path.

    :meth:`access_rules_for` is the only way to a decision: a gate is always
    handed the rule set for the profile whose session made the request, never
    the container.
    """

    def __init__(self, sections: dict[str, object], tranco_path: Path | None = None):
        # tranco_path is the Tranco snapshot that sits next to the allowlist
        # file; the loader/from_file pass it in. A bare dict construction (tests,
        # the import-time default) leaves it None -> the ~/.browden fallback.
        self.max_browser_sessions = DEFAULT_MAX_BROWSER_SESSIONS
        self.max_tabs_per_session = DEFAULT_MAX_TABS_PER_SESSION
        self.reap_interval_seconds = DEFAULT_REAP_INTERVAL_SECONDS
        infra = sections.get("infra")
        if isinstance(infra, dict):
            self.max_browser_sessions = int(
                infra.get("max_browser_sessions", DEFAULT_MAX_BROWSER_SESSIONS))
            self.max_tabs_per_session = int(
                infra.get("max_tabs_per_session", DEFAULT_MAX_TABS_PER_SESSION))
            self.reap_interval_seconds = int(
                infra.get("reap_interval_seconds", DEFAULT_REAP_INTERVAL_SECONDS))
        self._access_rules = BrowdenAccessRuleSet(sections, tranco_path)
        # Each profile's rules sit on top of the global set (base=), keyed by the
        # canonical profile path the session layer keys its Chrome sessions by.
        profiles = sections.get("profiles")
        self._profiles: dict[str, BrowdenAccessRuleSet] = {
            str(canonical_profile_dir(key)): BrowdenAccessRuleSet(
                body if isinstance(body, dict) else {}, tranco_path, base=self._access_rules)
            for key, body in (profiles or {}).items()}

    @classmethod
    def from_file(cls, path: Path) -> "BrowdenRuntimeConfiguration":
        return cls(yaml.safe_load(path.read_text()) or {},
                   tranco_path=path.parent / TRANCO_FILENAME)

    @property
    def access_rules(self) -> BrowdenAccessRuleSet:
        """The global rule set — what a profile with no ``profiles`` block gets.

        Not the one to gate a request with: use :meth:`access_rules_for`, which
        starts here and adds whatever the request's own profile is allowed.
        """
        return self._access_rules

    def access_rules_for(self, profile_dir: "str | Path") -> BrowdenAccessRuleSet:
        """The rule set that decides a request made in ``profile_dir``'s session.

        The profile's own set if it has a ``profiles`` entry (global rules plus
        its own), else the global set. ``profile_dir`` is normally the path the
        session is already keyed by (canonical — see
        :func:`~browden.common.profile.canonical_profile_dir`), which is a plain
        dict lookup; a spelling that misses is canonicalized and looked up once
        more, so a caller naming a profile with ``~`` or a relative path still
        finds it.

        A lookup that misses falls back to the global set, which is the narrow
        direction: a profile's rules are additive, so the fallback can only ever
        grant less than the intended entry would have.
        """
        if not self._profiles:
            return self._access_rules  # no profiles block — every config written before this
        key = str(profile_dir)
        scoped = self._profiles.get(key)
        if scoped is not None:
            return scoped  # the session's own (already canonical) path: a plain lookup
        try:
            return self._profiles.get(str(canonical_profile_dir(key)), self._access_rules)
        except (OSError, ValueError):
            # An unusable path can't name a profile; fall back to the global set
            # rather than failing the request open or crashing the gate.
            return self._access_rules

    def profile_dirs(self) -> list[str]:
        """The canonical profile paths this config scopes rules to (for logging)."""
        return sorted(self._profiles)
