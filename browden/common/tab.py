from dataclasses import dataclass


@dataclass
class TabInfo:
    """Information about an open browser tab.

    ``handle`` is the backend's raw, per-session tab handle. The
    server's cross-profile identifier — the customer-facing ``id`` — is composed
    from this plus the tab's profile namespace.
    """
    handle: str
    url: str
    title: str
    selected: bool
    profile_dir: str

    def as_dict(self, id: str) -> dict[str, str | bool]:
        """The wire shape for a tab.

        ``id`` is the customer-facing tab id (the server's cross-profile
        identifier); the raw ``handle`` is internal and deliberately
        not surfaced. ``selected`` is a real JSON boolean — never a
        stringified ``"True"``.
        """
        return {
            "id": id,
            "url": self.url,
            "title": self.title,
            "selected": self.selected,
            "profile_dir": self.profile_dir,
        }
