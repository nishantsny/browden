from dataclasses import dataclass


@dataclass
class TabInfo:
    """Information about an open browser tab."""
    id: str
    url: str
    title: str
    selected: bool

    def as_page_dict(self, profile_dir: str | None = None) -> dict:
        """The wire shape tools return for a tab (``id`` becomes ``tab_id``)."""
        d = {"tab_id": self.id, "url": self.url, "title": self.title, "selected": self.selected}
        if profile_dir is not None:
            d["profile_dir"] = profile_dir
        return d
