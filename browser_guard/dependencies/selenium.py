"""Anti-corruption wrapper around the PyPI `selenium` package."""
from selenium import webdriver
from selenium.common.exceptions import NoSuchWindowException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.support.ui import WebDriverWait

__all__ = ["webdriver", "ChromeOptions", "WebDriverWait", "NoSuchWindowException"]
