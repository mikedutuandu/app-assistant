"""Short-lived signed links for the local storage backend: `/v1/files/{key}?exp=&sig=`.

The signature is HMAC-SHA256 over `method:key:exp` (exp is a Unix time), keyed by FILES_SIGNING_KEY or the key kept in
the `settings` table. The link is the whole permission: no cookie, no token.
"""

import hashlib
import hmac
import secrets
import time

SETTING = "files_signing_key"


def sign(secret: str, method: str, key: str, exp: int) -> str:
    return hmac.new(secret.encode(), f"{method}:{key}:{exp}".encode(), hashlib.sha256).hexdigest()


def valid(secret: str, method: str, key: str, exp: str, sig: str, now: float | None = None) -> bool:
    """True for a signature made by `secret` for this method and key that has not expired. Constant-time compare."""
    try:
        when = int(exp)
    except ValueError:
        return False
    if when < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(sign(secret, method, key, when), sig)


async def load_signing_key(pool) -> str:
    """The generated key: made once, then shared by every instance through the database."""
    async with pool.connection() as conn:
        await conn.execute(
            "insert into settings (key, value) values (%s, %s) on conflict (key) do nothing",
            (SETTING, secrets.token_hex(32)),
        )
        cur = await conn.execute("select value from settings where key = %s", (SETTING,))
        row = await cur.fetchone()
    assert row is not None
    return row[0]
