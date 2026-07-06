# SPDX-FileCopyrightText: 2026 Nishant
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass


@dataclass
class TabInfo:
    """Information about an open browser tab.

    ``per_session_id`` is the backend's raw, per-session tab handle. The
    server's cross-profile identifier — the customer-facing ``id`` — is composed
    from this plus the tab's profile namespace.
    """
    per_session_id: str
    url: str
    title: str
    selected: bool
    profile_dir: str

    def as_dict(self, id: str) -> dict[str, str]:
        """The wire shape for a tab.

        ``id`` is the customer-facing tab id (the server's cross-profile
        identifier); the raw ``per_session_id`` is internal and deliberately
        not surfaced.
        """
        return {
            "id": id,
            "url": self.url,
            "title": self.title,
            "selected": str(self.selected),
            "profile_dir": self.profile_dir,
        }
