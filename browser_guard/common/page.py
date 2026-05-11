from dataclasses import dataclass


@dataclass
class PageInfo:
    """Information about an open browser tab."""
    id: str
    url: str
    title: str
    selected: bool
