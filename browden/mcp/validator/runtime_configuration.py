"""The runtime configuration browden runs under: one loaded allowlist file.

:class:`BrowdenRuntimeConfiguration` is what the loader builds from the allowlist
YAML and the refresher hot-swaps: the process-wide ``infra`` caps plus the
:class:`~.access_rule_set.BrowdenAccessRuleSet` every gate decides against.
"""
from pathlib import Path

import yaml

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

    :attr:`access_rules` is the only way to a decision: a gate is always handed
    the rule set that governs the request it is deciding, never the container.
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

    @classmethod
    def from_file(cls, path: Path) -> "BrowdenRuntimeConfiguration":
        return cls(yaml.safe_load(path.read_text()) or {},
                   tranco_path=path.parent / TRANCO_FILENAME)

    @property
    def access_rules(self) -> BrowdenAccessRuleSet:
        """The rule set every gate decides against."""
        return self._access_rules
