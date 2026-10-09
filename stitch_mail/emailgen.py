"""Email address derivation for the stitch-mail plugin.

Ported from the removed core ``email_generate_from_settings`` command
(``domains/email/commands.py``).  The core read the base address and
strategy from the host settings table; the plugin derives them from
the request params, falling back to the newest visible IMAP profile in
its own store.
"""

from __future__ import annotations

import hashlib
import random
import string
import time
from typing import Any


def generate_by_strategy(
    strategy: str,
    base_email: str = "",
    domain: str | None = None,
    email_pool: list[str] | None = None,
    custom_login_name: str | None = None,
) -> dict[str, Any]:
    """Generate an email for *strategy* — mirrors the core EmailGenerator."""
    strategy = strategy.lower().replace("-", "_").replace(" ", "_")
    tag = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]

    if strategy == "single":
        return {"email": base_email or "user@example.com", "strategy": "single"}
    if strategy == "plus_alias":
        if not base_email or "@" not in base_email:
            return {"error": "imapUser required for plus_alias"}
        local, dom = base_email.rsplit("@", 1)
        return {"email": f"{local}+{tag}@{dom}", "strategy": "plus_alias"}
    if strategy in ("catch_all", "catchall"):
        dom = domain or (
            base_email.rsplit("@", 1)[1] if "@" in base_email else "example.com"
        )
        prefix = custom_login_name or "".join(
            random.choices(string.ascii_lowercase, k=10)
        )
        return {"email": f"{prefix}@{dom}", "strategy": "catch_all"}
    if strategy in ("gmail_dot_alias", "gmaildotalias"):
        if not base_email or "@" not in base_email:
            return {"error": "imapUser required for gmail_dot_alias"}
        local, dom = base_email.rsplit("@", 1)
        if len(local) > 2:
            idx = random.randint(1, len(local) - 1)
            dotted = local[:idx] + "." + local[idx:]
        else:
            dotted = f"{local}.{tag[:4]}"
        return {"email": f"{dotted}@{dom}", "strategy": "gmail_dot_alias"}
    if strategy == "pool":
        if not email_pool:
            return {"error": "emailPool required for pool strategy"}
        return {"email": random.choice(email_pool), "strategy": "pool"}
    return {"email": f"user_{tag}@example.com", "strategy": strategy}


def test_strategies() -> list[list[Any]]:
    """Probe every strategy with fixed inputs — mirrors the core command."""
    results: list[list[Any]] = []
    for strategy, base_email, domain, pool in [
        ("single", "test.user@gmail.com", None, None),
        ("plus_alias", "test.user@gmail.com", None, None),
        ("catch_all", "", "mydomain.ru", None),
        ("gmail_dot_alias", "testuser@gmail.com", None, None),
        ("pool", "", None, ["user1@mail.ru", "user2@mail.ru"]),
    ]:
        results.append(
            [strategy, generate_by_strategy(strategy, base_email, domain, pool)]
        )
    return results
