"""Anti-corruption wrapper around the PyPI `selenium` package."""
from selenium import webdriver
from selenium.common.exceptions import (
    InvalidElementStateException,
    NoSuchElementException,
    NoSuchWindowException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

__all__ = [
    "webdriver",
    "ChromeOptions",
    "WebDriverWait",
    "By",
    "Keys",
    "NoSuchWindowException",
    "NoSuchElementException",
    "InvalidElementStateException",
]
