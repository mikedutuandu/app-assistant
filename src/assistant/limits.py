"""Atomic Postgres counters: a cap per key inside a time window.

One statement does the check and the add, so parallel callers can never pass the cap.
"""

import ipaddress
from datetime import datetime, timedelta

from psycopg_pool import AsyncConnectionPool

USER_MESSAGES_PER_HOUR = 30
DEMO_MESSAGES_PER_IP_DAY = 20
DEMO_USD_KEY = "demo-usd"


def user_key(app: str, workspace: str) -> str:
    return f"msg:{app}:{workspace}"


def ip_key(ip: str) -> str:
    """IPv4 as is; IPv6 reduced to its /64 network (one person owns a whole /64); anything else unchanged."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if address.version == 4:
        return ip
    if address.ipv4_mapped is not None:  # ::ffff:a.b.c.d is an IPv4 visitor on a dual-stack socket
        return str(address.ipv4_mapped)
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))


def demo_ip_key(ip: str) -> str:
    return f"demo-ip:{ip_key(ip)}"


_HIT = """
insert into counters (key, value, window_ends)
select %(key)s, %(amount)s::numeric, coalesce(%(now)s::timestamptz, now()) + %(window)s::interval
where %(amount)s::numeric <= %(cap)s::numeric
on conflict (key) do update set
    value = case when counters.window_ends <= coalesce(%(now)s::timestamptz, now())
                 then excluded.value else counters.value + excluded.value end,
    window_ends = case when counters.window_ends <= coalesce(%(now)s::timestamptz, now())
                       then excluded.window_ends else counters.window_ends end
where (case when counters.window_ends <= coalesce(%(now)s::timestamptz, now())
            then excluded.value else counters.value + excluded.value end) <= %(cap)s::numeric
returning 1
"""

_REMAINING = """
select %(cap)s::numeric - value
from counters
where key = %(key)s and window_ends > coalesce(%(now)s::timestamptz, now())
"""


async def hit(
    pool: AsyncConnectionPool,
    key: str,
    window: timedelta,
    cap: float,
    amount: float = 1,
    now: datetime | None = None,
) -> bool:
    """Add `amount` to the key's counter if the total stays within `cap`; False (nothing stored) otherwise.

    A counter whose window has ended starts again from `amount`. `now` is for tests (default: the database's now()).
    """
    params = {"key": key, "amount": amount, "cap": cap, "window": window, "now": now}
    async with pool.connection() as conn:
        cur = await conn.execute(_HIT, params)
        return await cur.fetchone() is not None


async def remaining(pool: AsyncConnectionPool, key: str, cap: float, now: datetime | None = None) -> float:
    """What is left under `cap` in the live window: `cap` when the key is missing or its window ended."""
    async with pool.connection() as conn:
        cur = await conn.execute(_REMAINING, {"key": key, "cap": cap, "now": now})
        row = await cur.fetchone()
    return float(cap) if row is None else float(row[0])
