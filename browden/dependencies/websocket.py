"""Anti-corruption wrapper around the PyPI `websocket-client` package.

Used for the browser-level DevTools socket only (see
``web_navigator/selenium_chrome/cdp.py``): chromedriver's own CDP session is
page-scoped and refuses the browser-scoped command that opens an incognito
browser context.
"""
from websocket import create_connection

__all__ = ["create_connection"]
