"""Raw IMAP operations for the stitch-mail plugin.

Blocking ``imaplib`` calls — the RPC serve loop is single-threaded, so
no per-session locking is needed.  ``poll_for_code`` is the wait_otp
engine ported from the core built-in EmailVerificationProvider
(``core/spi_builtin_email.py``): one fresh connection per poll
iteration, newest messages first, subject patterns tried in order.
"""

from __future__ import annotations

import email as email_mod
import imaplib
import re
import sys
import time
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, cast

#: Socket timeout — must stay under the host's 30s per-call kill.
_CONNECT_TIMEOUT_S = 20.0


def connect(
    host: str, port: int, username: str, password: str, use_tls: bool
) -> imaplib.IMAP4:
    """Open an IMAP connection and authenticate."""
    if use_tls:
        conn = cast(
            "imaplib.IMAP4", imaplib.IMAP4_SSL(host, port, timeout=_CONNECT_TIMEOUT_S)
        )
    else:
        conn = imaplib.IMAP4(host, port, timeout=_CONNECT_TIMEOUT_S)
    conn.login(username, password)
    return conn


def disconnect(conn: imaplib.IMAP4) -> None:
    """Logout, ignoring an already-dead connection."""
    try:
        conn.logout()
    except Exception:  # noqa: BLE001
        pass


def list_folders(conn: imaplib.IMAP4) -> list[dict[str, Any]]:
    """Return unique folders as [{id, path, name, kind, delimiter}]."""
    status, data = conn.list()
    if status != "OK":
        return []

    folders_by_path: dict[str, dict[str, Any]] = {}
    for item in data:
        if item is None:
            continue
        line = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        parts = line.split('"')
        if len(parts) >= 4:
            delimiter = parts[1] if len(parts) > 1 else "/"
            path = parts[3] if len(parts) > 3 else line
        else:
            delimiter = "/"
            path = line.split()[-1] if line.split() else "INBOX"

        flags = line.upper()
        if path.upper() == "INBOX" or "\\INBOX" in flags:
            kind = "inbox"
        elif "\\SENT" in flags:
            kind = "sent"
        elif "\\DRAFTS" in flags:
            kind = "drafts"
        elif "\\TRASH" in flags:
            kind = "trash"
        elif "\\JUNK" in flags or "\\SPAM" in flags:
            kind = "spam"
        elif "\\ALL" in flags:
            kind = "all"
        elif "\\ARCHIVE" in flags:
            kind = "archive"
        else:
            kind = "folder"

        name = path.rsplit(delimiter, 1)[-1] if delimiter else path
        folder = {
            "id": path,
            "path": path,
            "name": name,
            "kind": kind,
            "delimiter": delimiter,
        }
        canonical_path = path.strip().casefold()
        existing = folders_by_path.get(canonical_path)
        if existing is None or (existing["kind"] == "folder" and kind != "folder"):
            folders_by_path[canonical_path] = folder

    return list(folders_by_path.values())


def _imap_quote(value: str) -> str:
    """Escape a value for safe use inside IMAP quoted strings."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def search(conn: imaplib.IMAP4, query: dict[str, Any] | None) -> list[str]:
    """Run UID SEARCH and return message UIDs as strings."""
    criteria: list[str] = []
    q = query or {}
    if q.get("unreadOnly"):
        criteria.append("UNSEEN")
    if q.get("from"):
        criteria.append(f'FROM "{_imap_quote(str(q["from"]))}"')
    if q.get("to"):
        criteria.append(f'TO "{_imap_quote(str(q["to"]))}"')
    if q.get("subjectContains"):
        criteria.append(f'SUBJECT "{_imap_quote(str(q["subjectContains"]))}"')
    if q.get("bodyContains"):
        criteria.append(f'BODY "{_imap_quote(str(q["bodyContains"]))}"')
    if q.get("since"):
        criteria.append(f'SINCE {q["since"]}')
    search_str = " ".join(criteria) if criteria else "ALL"
    status, data = conn.uid("search", None, search_str)
    if status != "OK":
        return []
    ids = data[0].split() if data and data[0] else []
    return [uid.decode() if isinstance(uid, bytes) else str(uid) for uid in ids]


def fetch_message(conn: imaplib.IMAP4, uid: str) -> dict[str, Any]:
    """Fetch a single message by UID → dict matching the EmailMessage DTO."""
    status, data = conn.uid("fetch", uid, "(RFC822)")
    if status != "OK" or not data or data[0] is None:
        raise RuntimeError(f"failed to fetch message UID {uid}")

    raw = data[0][1] if isinstance(data[0], tuple) else data[0]
    msg = email_mod.message_from_bytes(raw)

    from_name, from_email = parseaddr(msg.get("From", ""))
    to_list = [
        {"name": n, "email": e}
        for n, e in [parseaddr(a) for a in (msg.get("To", "") or "").split(",")]
    ]
    subject = _decode_subject(msg.get("Subject", ""))

    text_body: str | None = None
    html_body: str | None = None
    attachments: list[dict[str, Any]] = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    text_body = cast("bytes", payload).decode("utf-8", errors="replace")
            elif ct == "text/html" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload:
                    html_body = cast("bytes", payload).decode("utf-8", errors="replace")
            elif "attachment" in disp:
                fname = part.get_filename() or "unnamed"
                payload = part.get_payload(decode=True)
                attachments.append(
                    {
                        "id": str(len(attachments) + 1),
                        "filename": fname,
                        "contentType": ct,
                        "size": len(payload) if payload else 0,
                    }
                )
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text_body = cast("bytes", payload).decode("utf-8", errors="replace")

    return {
        "id": uid,
        "providerMessageId": msg.get("Message-ID", uid),
        "from": {"name": from_name or None, "email": from_email},
        "to": to_list,
        "cc": [],
        "bcc": [],
        "subject": subject,
        "text": text_body,
        "html": html_body,
        "headers": dict(msg.items()),
        "attachments": attachments,
        "isRead": False,
        "receivedAt": msg.get("Date", ""),
    }


def mark_as_read(conn: imaplib.IMAP4, uid: str) -> None:
    conn.select("INBOX")
    status, _ = conn.uid("store", uid, "+FLAGS", "(\\Seen)")
    if status != "OK":
        raise RuntimeError(f"failed to mark message UID {uid} as read")


def delete_message(conn: imaplib.IMAP4, uid: str) -> None:
    conn.select("INBOX")
    status, _ = conn.uid("store", uid, "+FLAGS", "(\\Deleted)")
    if status != "OK":
        raise RuntimeError(f"failed to delete message UID {uid}")
    conn.expunge()


def _decode_subject(raw: str) -> str:
    parts = decode_header(raw)
    decoded: list[str] = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


# ── wait_otp poll (ported from core spi_builtin_email) ──────────────────────


def _extract_body(msg: Any) -> str:
    """Extract the text body from an ``email.message.Message``."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    text: str = payload.decode("utf-8", errors="replace")
                    return text
            elif ct == "text/html" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    body = re.sub(
                        r"<[^>]*>", " ",
                        payload.decode("utf-8", errors="replace"),
                    )
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode("utf-8", errors="replace")
    return body


def _poll_once(
    creds: dict[str, Any],
    subject_patterns: list[str],
    body_regex: str,
    recency_s: int,
    code_source: str,
    subject_code_regex: str,
) -> str | None:
    """Poll the mailbox once for a verification code.

    ``subject_patterns`` are tried IN ORDER (primary first, fallback
    after) so a broad fallback never shadows a precise primary match on
    the same poll iteration.  An empty list disables subject filtering.
    ``code_source`` selects where the code lives: ``"body"`` (default)
    matches ``body_regex`` against the body; ``"subject"`` matches
    ``subject_code_regex`` against the Subject header.
    """
    conn = connect(
        str(creds.get("host", "")),
        int(creds.get("port", 993)),
        str(creds.get("username", "")),
        str(creds.get("password", "")),
        bool(creds.get("useTls", creds.get("use_tls", True))),
    )
    try:
        conn.select("INBOX")
        status, data = conn.search(None, "ALL")
        if status != "OK" or not data[0]:
            return None
        msg_ids = data[0].split()
        subject_res = [re.compile(p) for p in subject_patterns if p]
        fetched: list[tuple[str, str]] = []
        for msg_id in reversed(msg_ids[-10:]):
            _, msg_data = conn.fetch(msg_id, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            msg = email_mod.message_from_bytes(msg_data[0][1])
            try:
                msg_date = parsedate_to_datetime(msg["Date"])
                if time.time() - msg_date.timestamp() > recency_s:
                    continue
            except Exception:  # noqa: BLE001
                pass
            fetched.append((msg.get("Subject", ""), _extract_body(msg)))

        if code_source == "subject":
            subject_code_re = re.compile(subject_code_regex or r"(\d{6})")
            for subject, _body in fetched:
                m = subject_code_re.search(subject)
                if m:
                    return m.group(1) if m.groups() else m.group(0)
            return None

        patterns: list[re.Pattern[str] | None] = (
            list(subject_res) if subject_res else [None]
        )
        for subject_re in patterns:
            for subject, body in fetched:
                if subject_re is not None and not subject_re.search(subject):
                    continue
                match = re.search(body_regex, body)
                if match:
                    return match.group(1) if match.groups() else match.group(0)
        return None
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def poll_for_code(
    creds: dict[str, Any],
    subject_patterns: list[str],
    body_regex: str,
    recency_s: int,
    timeout_s: float,
    poll_interval_s: float,
    code_source: str = "body",
    subject_code_regex: str = "",
) -> str:
    """Poll the mailbox for a verification code until *timeout_s*.

    Returns the code, or ``""`` on genuine timeout — the caller (host
    SPI bridge) treats ``""`` as "no code", never as an error.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            code = _poll_once(
                creds,
                subject_patterns,
                body_regex,
                recency_s,
                code_source,
                subject_code_regex,
            )
            if code:
                return code
        except Exception as exc:  # noqa: BLE001
            print(f"stitch-mail: imap poll error: {exc}", file=sys.stderr)
        time.sleep(poll_interval_s)
    return ""
