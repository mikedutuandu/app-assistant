import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from assistant.db import migrate
from assistant.limits import (
    DEMO_USD_KEY,
    demo_ip_key,
    hit,
    ip_key,
    remaining,
    user_key,
)

pytestmark = pytest.mark.db

HOUR = timedelta(hours=1)
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def test_cap_blocks_at_limit(pool):
    await migrate(pool)
    assert [await hit(pool, "k", HOUR, 3) for _ in range(5)] == [True, True, True, False, False]
    assert await remaining(pool, "k", 3) == 0


async def test_amount_over_cap_is_refused_and_not_stored(pool):
    await migrate(pool)
    assert await hit(pool, "k", HOUR, 1.0, amount=2.5) is False
    assert await remaining(pool, "k", 1.0) == 1.0
    assert await hit(pool, "k", HOUR, 1.0, amount=0.4) is True
    assert await hit(pool, "k", HOUR, 1.0, amount=0.7) is False
    assert await remaining(pool, "k", 1.0) == pytest.approx(0.6)


async def test_window_resets(pool):
    await migrate(pool)
    assert await hit(pool, "k", HOUR, 1, now=T0) is True
    assert await hit(pool, "k", HOUR, 1, now=T0 + timedelta(minutes=59)) is False
    assert await remaining(pool, "k", 1, now=T0 + timedelta(minutes=59)) == 0
    later = T0 + HOUR
    assert await remaining(pool, "k", 1, now=later) == 1
    assert await hit(pool, "k", HOUR, 1, now=later) is True
    assert await hit(pool, "k", HOUR, 1, now=later) is False


async def test_expired_window_with_amount_over_cap_keeps_old_row(pool):
    await migrate(pool)
    assert await hit(pool, "k", HOUR, 1, now=T0) is True
    assert await hit(pool, "k", HOUR, 1, amount=5, now=T0 + HOUR) is False


async def test_remaining_missing_key_is_cap(pool):
    await migrate(pool)
    assert await remaining(pool, "nope", 7) == 7


async def test_parallel_hits_never_exceed(pool):
    await migrate(pool)
    results = await asyncio.gather(*(hit(pool, "k", HOUR, 10) for _ in range(20)))
    assert results.count(True) == 10
    assert results.count(False) == 10
    assert await remaining(pool, "k", 10) == 0


def test_ipv6_keys_on_64():
    assert ip_key("203.0.113.9") == "203.0.113.9"
    assert ip_key("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == "2001:db8:1:2::/64"
    assert ip_key("2001:db8:1:2:1::1") == ip_key("2001:db8:1:2:ffff::2")
    assert ip_key("::ffff:203.0.113.9") == "203.0.113.9"
    assert ip_key("::ffff:198.51.100.1") == "198.51.100.1"
    assert ip_key("not-an-ip") == "not-an-ip"


def test_key_builders():
    assert user_key("hookplaybook", "ws1") == "msg:hookplaybook:ws1"
    assert demo_ip_key("2001:db8:1:2::5") == "demo-ip:2001:db8:1:2::/64"
    assert DEMO_USD_KEY == "demo-usd"
