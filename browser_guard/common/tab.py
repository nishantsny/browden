from dataclasses import dataclass


@dataclass
class TabInfo:
    """Information about an open browser tab."""
    id: str
    url: str
    title: str
    selected: bool
