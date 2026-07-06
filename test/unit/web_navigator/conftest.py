# SPDX-FileCopyrightText: 2026 Nishant
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the web_navigator unit tests.

The three modules under this directory all need a deterministic clock to test
TTL / idle-reap behaviour without sleeping. They previously each defined an
identical ``FakeClock`` class; this module is the single source of truth.
"""
import pytest


class _FakeClock:
    """Callable clock with a mutable ``.t``. Use ``clock.t += seconds`` to advance."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def fake_clock():
    """Factory fixture — ``clock = fake_clock()`` (default ``t=1000.0``) or ``fake_clock(0.0)``."""
    return _FakeClock
