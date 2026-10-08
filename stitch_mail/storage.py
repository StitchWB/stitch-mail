"""SQLite storage for the stitch-mail plugin.

The plugin owns its own SQLite database at ``db_path`` (received in the
``plugin.init`` handshake).  Tables mirror the core email_inbox profile
cache, sync states, and email counters so the plugin can serve mail
commands independently during the migration window (dual-format, plan
todo 15/16).  Built-in domain stays as fallback until todo 24.
"""

# _generated_by: stitch_plugin_tools scaffold v3

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode for concurrent reads."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def migrate(db_path: str) -> None:
    """Create plugin tables if they do not exist (raw_sql migration).

    Tables:
      - mail_profiles: mirror cache of inbox profiles (id, email, provider,
        credentials_json, label, account_id, connect_input_json, owner_id,
        created_at, updated_at).  Rows written by older plugin versions
        (label/account_id/connect_input_json missing) are backfilled here.
      - mail_sync_states: per-profile sync state (profile_id, status,
        last_sync_at).
      - mail_counters: per-(provider, strategy) persistent email counter.
    """
    conn = _connect(db_path)
    try:
        counter_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(mail_counters)").fetchall()
        }
        if counter_cols and "strategy" not in counter_cols:
            conn.execute("ALTER TABLE mail_counters RENAME TO mail_counters_v1")
        profile_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(mail_profiles)").fetchall()
        }
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mail_profiles (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT 'imap',
                credentials_json TEXT NOT NULL DEFAULT '{}',
                label TEXT NOT NULL DEFAULT '',
                account_id TEXT NOT NULL DEFAULT '',
                connect_input_json TEXT NOT NULL DEFAULT '',
                owner_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS mail_sync_states (
                profile_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'idle',
                last_sync_at TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS mail_counters (
                provider TEXT NOT NULL,
                strategy TEXT NOT NULL DEFAULT 'default',
                counter INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (provider, strategy)
            );
            """
        )
        for col in ("label", "account_id", "connect_input_json"):
            if profile_cols and col not in profile_cols:
                conn.execute(
                    f"ALTER TABLE mail_profiles ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        _backfill_profiles(conn)
        if counter_cols and "strategy" not in counter_cols:
            conn.execute(
                """
                INSERT OR IGNORE INTO mail_counters
                    (provider, strategy, counter, updated_at)
                SELECT provider, 'default', counter, updated_at
                FROM mail_counters_v1
                """
            )
            conn.execute("DROP TABLE mail_counters_v1")
        conn.commit()
    finally:
        conn.close()


def _backfill_profiles(conn: sqlite3.Connection) -> None:
    """Derive label/account_id/connect_input_json for legacy rows.

    credentials_json holds the bare credentials value; unparsable or
    non-dict payloads fall back to an empty value.
    """
    rows = conn.execute(
        "SELECT id, email, provider, credentials_json, label, account_id, "
        "connect_input_json FROM mail_profiles "
        "WHERE label = '' OR account_id = '' OR connect_input_json = ''"
    ).fetchall()
    for row in rows:
        email = str(row["email"])
        provider = str(row["provider"])
        label = str(row["label"]) or email
        account_id = str(row["account_id"]) or f"{provider}:{email}"
        connect_input_json = str(row["connect_input_json"]) or json.dumps(
            {
                "provider": provider,
                "accountId": account_id,
                "credentials": {
                    "type": provider,
                    "value": _load_json_dict(row["credentials_json"]),
                },
                "options": None,
            }
        )
        conn.execute(
            "UPDATE mail_profiles SET label = ?, account_id = ?, "
            "connect_input_json = ? WHERE id = ?",
            (label, account_id, connect_input_json, row["id"]),
        )


# ── Visibility helpers (mirror stitch-totp fetch_visible pattern) ────────────


def _visible_where(uid: int | None) -> tuple[str, list[Any]]:
    """WHERE clause: own OR instance-shared (NULL owner)."""
    if uid is None:
        return "owner_id IS NULL", []
    return "owner_id IS NULL OR owner_id = ?", [uid]


def _fetch_visible(
    conn: sqlite3.Connection, profile_id: str, uid: int | None
) -> sqlite3.Row | None:
    where, args = _visible_where(uid)
    return conn.execute(
        f"SELECT * FROM mail_profiles WHERE id = ? AND ({where})",
        [profile_id, *args],
    ).fetchone()


# ── Profiles ─────────────────────────────────────────────────────────────────


def list_profiles(db_path: str, owner_id: int | None = None) -> list[dict[str, Any]]:
    """Return mail profiles visible to *owner_id* (shared + owned)."""
    conn = _connect(db_path)
    try:
        where, args = _visible_where(owner_id)
        rows = conn.execute(
            f"SELECT * FROM mail_profiles WHERE {where} ORDER BY updated_at DESC",
            args,
        ).fetchall()
        return [_row_to_profile(r, owner_id) for r in rows]
    finally:
        conn.close()


def get_profile(
    db_path: str, profile_id: str, owner_id: int | None = None
) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = _fetch_visible(conn, profile_id, owner_id)
        return _row_to_profile(row, owner_id) if row else None
    finally:
        conn.close()


def upsert_profile(
    db_path: str, data: dict[str, Any], owner_id: int | None = None
) -> dict[str, Any]:
    """Create or update a profile, stamping the CALLER's owner_id.

    Accepts the frontend upsert shape ``{id?, label?, connectInput}`` and
    the legacy ``{id?, email, provider, credentials}`` shape; both are
    persisted to the full column set and the full contract row is
    returned.  New rows get owner_id = caller (None for guest →
    shared/NULL).  On conflict, the existing owner_id is preserved
    (never overwritten from the request body) and the update is only
    allowed when the existing row is visible to the caller (shared or
    owned) — mirroring totp update_key semantics.
    """
    pid = str(data.get("id") or data.get("profileId") or uuid.uuid4().hex[:12])
    connect_input = _extract_connect_input(data)
    provider = str(connect_input.get("provider", "imap"))
    account_id = str(connect_input.get("accountId", ""))
    creds_type, creds_value = _split_credentials(provider, connect_input.get("credentials"))
    creds_json = json.dumps(creds_value)
    email = str(data.get("email") or _email_from_credentials(creds_type, creds_value))
    connect_input_json = json.dumps(connect_input)
    input_label = data.get("label")
    now = datetime.now(UTC).isoformat()
    conn = _connect(db_path)
    try:
        existing = _fetch_visible(conn, pid, owner_id)
        if existing is None:
            # Row exists but is not visible to caller → refuse.
            exists = conn.execute(
                "SELECT 1 FROM mail_profiles WHERE id = ?", (pid,)
            ).fetchone()
            if exists:
                raise ValueError(f"profile not visible to caller: {pid}")
            raw_label = input_label if input_label is not None else ""
        else:
            raw_label = input_label if input_label is not None else existing["label"]
        label = str(raw_label or "").strip() or email or account_id or pid
        if existing is None:
            # New row: stamp caller's owner_id (None for guest = shared).
            conn.execute(
                "INSERT INTO mail_profiles "
                "(id, email, provider, credentials_json, label, account_id, "
                "connect_input_json, owner_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    email,
                    provider,
                    creds_json,
                    label,
                    account_id,
                    connect_input_json,
                    owner_id,
                    now,
                ),
            )
        else:
            # Existing visible row: update, preserve owner_id.
            conn.execute(
                "UPDATE mail_profiles SET email = ?, provider = ?, "
                "credentials_json = ?, label = ?, account_id = ?, "
                "connect_input_json = ?, updated_at = ? WHERE id = ?",
                (
                    email,
                    provider,
                    creds_json,
                    label,
                    account_id,
                    connect_input_json,
                    now,
                    pid,
                ),
            )
        conn.commit()
        row = _fetch_visible(conn, pid, owner_id)
    finally:
        conn.close()
    return _row_to_profile(row, owner_id)


def delete_profile(
    db_path: str, profile_id: str, owner_id: int | None = None
) -> bool:
    """Delete a profile visible to the caller (shared or owned)."""
    conn = _connect(db_path)
    try:
        where, args = _visible_where(owner_id)
        cur = conn.execute(
            f"DELETE FROM mail_profiles WHERE id = ? AND ({where})",
            [profile_id, *args],
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def claim_profile(
    db_path: str, profile_id: str, owner_id: int
) -> dict[str, Any]:
    """Assign *owner_id* to a shared (NULL-owner) profile.

    Raises ValueError when the profile does not exist or already has an
    owner — claiming never transfers ownership between users.
    """
    now = datetime.now(UTC).isoformat()
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT owner_id FROM mail_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"profile not found: {profile_id}")
        if row["owner_id"] is not None:
            raise ValueError(f"profile already owned: {profile_id}")
        conn.execute(
            "UPDATE mail_profiles SET owner_id = ?, updated_at = ? WHERE id = ?",
            (owner_id, now, profile_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"success": True}


# ── Sync states ───────────────────────────────────────────────────────────────


def get_sync_state(
    db_path: str, profile_id: str, owner_id: int | None = None
) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        where, args = _visible_where(owner_id)
        row = conn.execute(
            f"SELECT * FROM mail_sync_states WHERE profile_id = ? "
            f"AND profile_id IN (SELECT id FROM mail_profiles WHERE {where})",
            [profile_id, *args],
        ).fetchone()
        if not row:
            return None
        return {
            "profileId": row["profile_id"],
            "status": row["status"],
            "lastSyncAt": row["last_sync_at"],
        }
    finally:
        conn.close()


def upsert_sync_state(
    db_path: str, data: dict[str, Any], owner_id: int | None = None
) -> dict[str, Any]:
    pid = str(data.get("profileId") or data.get("profile_id") or "")
    if not pid:
        return {"success": False, "error": "profileId is required"}
    status = str(data.get("status", "synced"))
    last_sync = str(data.get("lastSyncAt") or datetime.now(UTC).isoformat())
    now = datetime.now(UTC).isoformat()
    conn = _connect(db_path)
    try:
        where, args = _visible_where(owner_id)
        visible = conn.execute(
            f"SELECT 1 FROM mail_profiles WHERE id = ? AND ({where})",
            [pid, *args],
        ).fetchone()
        if not visible:
            raise ValueError(f"profile not visible to caller: {pid}")
        conn.execute(
            """
            INSERT INTO mail_sync_states (profile_id, status, last_sync_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                status = excluded.status,
                last_sync_at = excluded.last_sync_at,
                updated_at = excluded.updated_at
            """,
            (pid, status, last_sync, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"profileId": pid, "status": status, "lastSyncAt": last_sync}


# ── Counters ─────────────────────────────────────────────────────────────────


def increment_counter(
    db_path: str, provider: str = "default", strategy: str = "default"
) -> int:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO mail_counters (provider, strategy, counter, updated_at)
            VALUES (?, ?, 1, datetime('now'))
            ON CONFLICT(provider, strategy) DO UPDATE SET
                counter = mail_counters.counter + 1,
                updated_at = datetime('now')
            """,
            (provider, strategy),
        )
        conn.commit()
        row = conn.execute(
            "SELECT counter FROM mail_counters WHERE provider = ? AND strategy = ?",
            (provider, strategy),
        ).fetchone()
        return int(row["counter"]) if row else 0
    finally:
        conn.close()


def get_counter(db_path: str, provider: str, strategy: str) -> int:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT counter FROM mail_counters WHERE provider = ? AND strategy = ?",
            (provider, strategy),
        ).fetchone()
        return int(row["counter"]) if row else 0
    finally:
        conn.close()


def set_counter(db_path: str, provider: str, strategy: str, value: int) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO mail_counters (provider, strategy, counter, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(provider, strategy) DO UPDATE SET
                counter = excluded.counter,
                updated_at = datetime('now')
            """,
            (provider, strategy, value),
        )
        conn.commit()
    finally:
        conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_connect_input(data: dict[str, Any]) -> dict[str, Any]:
    """Frontend ``connectInput`` when present, else the legacy shape
    reconstructed from email/provider/credentials."""
    ci = data.get("connectInput", data.get("connect_input"))
    if isinstance(ci, dict) and ci:
        return ci
    provider = str(data.get("provider", "imap"))
    email = str(data.get("email", ""))
    creds_type, creds_value = _split_credentials(
        provider, data.get("credentials", data.get("credentialsJson", {}))
    )
    return {
        "provider": provider,
        "accountId": f"{provider}:{email}" if email else "",
        "credentials": {"type": creds_type, "value": creds_value},
        "options": None,
    }


def _split_credentials(provider: str, creds: Any) -> tuple[str, dict[str, Any]]:
    """Split a credentials payload into (type, bare value dict).

    Accepts the wrapped ``{type, value}`` form and the bare value form;
    anything else degrades to an empty value.
    """
    if isinstance(creds, dict) and isinstance(creds.get("value"), dict):
        return str(creds.get("type") or provider), creds["value"]
    value = creds if isinstance(creds, dict) else {}
    return provider, value


def _email_from_credentials(provider: str, value: dict[str, Any]) -> str:
    if provider == "mail_tm":
        return str(value.get("address", ""))
    return str(value.get("username", ""))


def _row_connect_input(row: sqlite3.Row) -> dict[str, Any]:
    raw = row["connect_input_json"]
    if raw:
        ci = _load_json_dict(raw)
        if ci:
            return ci
    provider = str(row["provider"])
    email = str(row["email"])
    account_id = str(row["account_id"]) or f"{provider}:{email}"
    return {
        "provider": provider,
        "accountId": account_id,
        "credentials": {
            "type": provider,
            "value": _load_json_dict(row["credentials_json"]),
        },
        "options": None,
    }


def _row_to_profile(row: sqlite3.Row, uid: int | None = None) -> dict[str, Any]:
    owner = row["owner_id"]
    return {
        "id": row["id"],
        "label": row["label"] or row["email"] or row["account_id"],
        "provider": row["provider"],
        "accountId": row["account_id"] or f"{row['provider']}:{row['email']}",
        "connectInput": _row_connect_input(row),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "mine": uid is not None and owner == uid,
        "shared": owner is None,
    }
