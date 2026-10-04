"""A pass-through ``WriteGate`` for e2e tests of the write *primitives*.

These tests drive ``BrowserSessionManager.click`` / ``insert_text`` /
``press_key`` against real Chrome to pin what the backend itself does (fire the
onclick, refuse a character key, refuse an ambiguous live match). The policy
gates are not under test here — they are unit-tested with real rules — so the
session is handed a gate that admits everything.
"""
from pathlib import Path

from browden.mcp.validator import AdmittedFile, ReadGate, UploadFileGate, WriteGate

OPEN_GATE = WriteGate(check_page=lambda url: None,
                      check_element=lambda url, css_selector, found: None)

# Likewise for reads and navigation: these tests load ``data:`` pages, which the
# real read gate refuses by scheme, to pin what the session and backend do.
OPEN_READ_GATE = ReadGate(check_page=lambda url: None)


def open_upload_gate(file_path) -> UploadFileGate:
    """An ``upload_file`` gate that admits ``file_path`` without judging it.

    ``upload_file`` takes no path argument: the file is named to its gate, which
    normally resolves and bounds it (``validate_upload_path``). For the tests of
    the *primitive*, where the policy is not under test, this admits the given
    path as-is — the real gate is exercised by its own tests, and by the two e2e
    cases that build one.
    """
    admitted = AdmittedFile()
    admitted.admit(Path(file_path))
    return UploadFileGate(check_page=lambda url: None,
                          check_element=lambda url, css_selector, found: None,
                          admitted=admitted)
