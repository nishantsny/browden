# SPDX-FileCopyrightText: 2026 Nishant
# SPDX-License-Identifier: Apache-2.0

"""Anti-corruption wrapper around the PyPI `selenium` package."""
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, NoSuchWindowException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

__all__ = [
    "webdriver",
    "ChromeOptions",
    "WebDriverWait",
    "By",
    "NoSuchWindowException",
    "NoSuchElementException",
]
