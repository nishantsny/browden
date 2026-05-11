"""Anti-corruption wrapper around the PyPI `selenium` package."""
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions

__all__ = ["webdriver", "ChromeOptions"]
