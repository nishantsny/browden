"""Anti-corruption wrapper around `beautifulsoup4` (and its `soupsieve` CSS engine).

All in-project code imports HTML-parsing symbols from here, never directly from
the third-party packages. `SelectorSyntaxError` is raised by soupsieve when a CSS
selector is malformed; we re-export it so `dom/query.py` can translate it into a
clean tool error.
"""
from bs4 import BeautifulSoup
from soupsieve.util import SelectorSyntaxError

__all__ = ["BeautifulSoup", "SelectorSyntaxError"]
