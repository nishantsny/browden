# SPDX-FileCopyrightText: 2026 Nishant
# SPDX-License-Identifier: Apache-2.0

from browser_guard.web_navigator.registry import TabRegistry


def test_idle_pages_uses_ttl_and_clock(fake_clock):
    clock = fake_clock()
    reg = TabRegistry(clock=clock)
    reg.touch("fresh")
    clock.t += 10
    reg.touch("staleish")
    clock.t += 3600  # 'fresh' is now 3610s old, 'staleish' 3600s old

    idle = set(reg.idle_pages(3600))
    assert idle == {"fresh", "staleish"}

    # bump it forward only a little: nothing idle yet
    reg2 = TabRegistry(clock=clock)
    reg2.touch("x")
    clock.t += 100
    assert reg2.idle_pages(3600) == []


def test_touch_resets_age(fake_clock):
    clock = fake_clock()
    reg = TabRegistry(clock=clock)
    reg.touch("p")
    clock.t += 5000
    assert reg.idle_pages(3600) == ["p"]
    reg.touch("p")  # reset
    assert reg.idle_pages(3600) == []


def test_forget_removes_page(fake_clock):
    reg = TabRegistry(clock=fake_clock())
    reg.touch("p")
    reg.forget("p")
    reg.forget("p")  # idempotent
    assert reg.idle_pages(0) == []


def test_now_argument_overrides_clock(fake_clock):
    reg = TabRegistry(clock=fake_clock(0.0))
    reg.touch("p", now=100.0)
    assert reg.idle_pages(50, now=200.0) == ["p"]
    assert reg.idle_pages(50, now=120.0) == []
