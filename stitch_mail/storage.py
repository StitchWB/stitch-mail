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
        credentials_json, owner_id, created_at, updated_at).
      - mail_sync_states: per-profile sync state (profile_id, status,
        last_sync_at).
      - mail_counters: per-provider persistent email counter.
    """
    conn = _connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mail_profiles (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT 'imap',
                credentials_json TEXT NOT NULL DEFAULT '{}',
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
                provider TEXT PRIMARY KEY,
                counter INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


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
        return [_row_to_profile(r) for r in rows]
    finally:
        conn.close()


def get_profile(
    db_path: str, profile_id: str, owner_id: int | None = None
) -> dict[str, Any] | None:
    conn = _connect(db_path)
    try:
        row = _fetch_visible(conn, profile_id, owner_id)
        return _row_to_profile(row) if row else None
    finally:
        conn.close()


def upsert_profile(
    db_path: str, data: dict[str, Any], owner_id: int | None = None
) -> dict[str, Any]:
    """Create or update a profile, stamping the CALLER's owner_id.

    New rows get owner_id = caller (None for guest → shared/NULL).  On
    conflict, the existing owner_id is preserved (never overwritten from
    the request body) and the update is only allowed when the existing
    row is visible to the caller (shared or owned) — mirroring totp
    update_key semantics.
    """
    pid = str(data.get("id") or data.get("profileId") or uuid.uuid4().hex[:12])
    email = str(data.get("email", ""))
    provider = str(data.get("provider", "imap"))
    creds = data.get("credentials", data.get("credentialsJson", {}))
    creds_json = json.dumps(creds) if isinstance(creds, dict) else str(creds)
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
            # New row: stamp caller's owner_id (None for guest = shared).
            conn.execute(
                "INSERT INTO mail_profiles "
                "(id, email, provider, credentials_json, owner_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pid, email, provider, creds_json, owner_id, now),
            )
        else:
            # Existing visible row: update, preserve owner_id.
            conn.execute(
                "UPDATE mail_profiles SET email = ?, provider = ?, "
                "credentials_json = ?, updated_at = ? WHERE id = ?",
                (email, provider, creds_json, now, pid),
            )
        conn.commit()
    finally:
        conn.close()
    return {"id": pid, "email": email, "provider": provider}


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


def increment_counter(db_path: str, provider: str = "default") -> int:
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO mail_counters (provider, counter, updated_at)
            VALUES (?, 1, datetime('now'))
            ON CONFLICT(provider) DO UPDATE SET
                counter = mail_counters.counter + 1,
                updated_at = datetime('now')
            """,
            (provider,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT counter FROM mail_counters WHERE provider = ?", (provider,)
        ).fetchone()
        return int(row["counter"]) if row else 0
    finally:
        conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _row_to_profile(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "provider": row["provider"],
        "owner_id": row["owner_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
