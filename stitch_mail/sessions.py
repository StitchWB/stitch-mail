"""In-memory mailbox sessions for the stitch-mail plugin.

Ported from the removed core ``email_inbox/service.py`` session half:
connect dispatches to the imap or mail_tm provider, session commands
run against the stored connection.  The RPC serve loop is
single-threaded, so a plain dict replaces the core's lock-guarded
async session store.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from . import imap, mailtm

IMAP_CAPABILITIES: dict[str, bool] = {
    "canDelete": True,
    "canMarkAsRead": True,
    "canSearchBody": True,
    "canDownloadAttachments": False,
    "canListFolders": True,
}

#: Poll ceiling — the host kills plugin calls at 30s, so waits must give up first.
_MAX_WAIT_S = 25.0

_sessions: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _get(session_id: str) -> dict[str, Any]:
    sess = _sessions.get(session_id)
    if not sess:
        raise RuntimeError(f"session not found: {session_id}")
    return sess


def connect(input_data: dict[str, Any]) -> dict[str, Any]:
    """Connect to a mailbox and return the session descriptor."""
    provider = str(input_data.get("provider", "imap"))
    account_id = str(input_data.get("accountId", "")).strip()
    creds = input_data.get("credentials", {}) or {}

    if provider == "mail_tm":
        value = creds.get("value", creds) or {}
        address = str(value.get("address", ""))
        password = str(value.get("password", ""))
        base_url = value.get("baseUrl") or value.get("base_url") or None
        if not address or not password:
            raise ValueError("Mail.tm credentials require address and password")
        service = mailtm.login(address, password, base_url)
        session_id = str(uuid.uuid4())
        _sessions[session_id] = {"provider": "mail_tm", "service": service}
        return {
            "sessionId": session_id,
            "provider": provider,
            "accountId": account_id,
            "capabilities": dict(mailtm.MAILTM_CAPABILITIES),
            "connectedAt": _now_iso(),
        }

    if provider != "imap":
        raise ValueError(f"provider not supported: {provider}")

    value = creds.get("value", creds) or {}
    host = str(value.get("host", ""))
    port = int(value.get("port", 993))
    username = str(value.get("username", ""))
    password = str(value.get("password", ""))
    use_tls = bool(value.get("useTls", value.get("use_tls", True)))
    if not host or not username:
        raise ValueError("IMAP credentials require host and username")

    conn = imap.connect(host, port, username, password, use_tls)
    session_id = str(uuid.uuid4())
    _sessions[session_id] = {"provider": "imap", "conn": conn}
    return {
        "sessionId": session_id,
        "provider": provider,
        "accountId": account_id,
        "capabilities": dict(IMAP_CAPABILITIES),
        "connectedAt": _now_iso(),
    }


def disconnect(session_id: str) -> None:
    sess = _sessions.pop(session_id, None)
    if not sess:
        return
    if sess["provider"] == "mail_tm":
        mailtm.disconnect(sess["service"])
    else:
        imap.disconnect(sess["conn"])


def list_messages(
    session_id: str, query: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    sess = _get(session_id)
    if sess["provider"] == "mail_tm":
        return mailtm.list_messages(sess["service"], query)

    limit = (query or {}).get("limit")
    conn = sess["conn"]
    conn.select("INBOX", readonly=True)
    uids = imap.search(conn, query)
    if limit:
        uids = uids[-int(limit):]

    messages: list[dict[str, Any]] = []
    for uid in uids:
        try:
            messages.append(imap.fetch_message(conn, uid))
        except Exception:  # noqa: BLE001
            pass
    return messages


def list_folders(session_id: str) -> list[dict[str, Any]]:
    sess = _get(session_id)
    if sess["provider"] == "mail_tm":
        return [dict(mailtm.INBOX_FOLDER)]
    return imap.list_folders(sess["conn"])


def get_by_id(session_id: str, message_id: str) -> dict[str, Any] | None:
    sess = _get(session_id)
    if sess["provider"] == "mail_tm":
        return mailtm.get_message(sess["service"], message_id)
    conn = sess["conn"]
    try:
        conn.select("INBOX", readonly=True)
        return imap.fetch_message(conn, message_id)
    except Exception:  # noqa: BLE001
        return None


def wait_for_email(
    session_id: str,
    query: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Poll the mailbox until a message matches, then return it.

    The wait is capped at ``_MAX_WAIT_S`` regardless of the requested
    timeoutMs — the host kills the call at 30s, so a longer poll would
    surface as an RPC kill instead of a clean timeout error.
    """
    opts = options or {}
    timeout_ms = opts.get("timeoutMs", opts.get("timeout_ms", 120_000))
    poll_ms = opts.get("pollIntervalMs", opts.get("poll_interval_ms", 3_000))
    timeout_s = min(float(timeout_ms) / 1000.0, _MAX_WAIT_S)
    poll_s = max(float(poll_ms) / 1000.0, 0.1)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        messages = list_messages(session_id, query)
        if messages:
            return messages[0]
        time.sleep(poll_s)
    raise RuntimeError("Email not found within timeout")


def mark_as_read(session_id: str, message_id: str) -> None:
    sess = _get(session_id)
    if sess["provider"] == "mail_tm":
        mailtm.mark_seen(sess["service"], message_id)
        return
    imap.mark_as_read(sess["conn"], message_id)


def delete_message(session_id: str, message_id: str) -> None:
    sess = _get(session_id)
    if sess["provider"] == "mail_tm":
        mailtm.delete_message(sess["service"], message_id)
        return
    imap.delete_message(sess["conn"], message_id)


def get_capabilities(session_id: str) -> dict[str, bool]:
    sess = _get(session_id)
    if sess["provider"] == "mail_tm":
        return dict(mailtm.MAILTM_CAPABILITIES)
    return dict(IMAP_CAPABILITIES)


def get_provider_catalog() -> list[dict[str, Any]]:
    return [
        {
            "provider": "imap",
            "displayName": "IMAP",
            "available": True,
            "capabilities": dict(IMAP_CAPABILITIES),
            "supportsProfileConnect": True,
        },
        {
            "provider": "mail_tm",
            "displayName": "Mail.tm",
            "available": True,
            "capabilities": dict(mailtm.MAILTM_CAPABILITIES),
            "supportsProfileConnect": True,
        },
    ]
