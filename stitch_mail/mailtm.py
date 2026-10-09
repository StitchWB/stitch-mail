"""Mail.tm REST client for the stitch-mail plugin (httpx).

Ported from the removed core ``email_inbox/mailtm_provider.py`` +
``autoreg/services/mailtm.py``.  Message dicts mirror
``imap.fetch_message`` so the frontend cannot tell providers apart.
"""

from __future__ import annotations

import os
import random
import string
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.mail.tm"

#: Per-request timeout — must stay under the host's 30s per-call kill.
_REQUEST_TIMEOUT_S = 10.0

#: Test/CI override for the mail.tm base URL (stub upstream wiring).
_BASE_URL_ENV = "STITCH_MAIL_MAILTM_URL"

MAILTM_CAPABILITIES: dict[str, bool] = {
    "canDelete": True,
    "canMarkAsRead": True,
    "canSearchBody": False,
    "canDownloadAttachments": False,
    "canListFolders": False,
}

INBOX_FOLDER: dict[str, Any] = {
    "id": "INBOX",
    "path": "INBOX",
    "name": "INBOX",
    "kind": "inbox",
    "delimiter": "/",
}


def _base_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    return os.environ.get(_BASE_URL_ENV) or DEFAULT_BASE_URL


def _open_client(base_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        timeout=_REQUEST_TIMEOUT_S,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )


def login(address: str, password: str, base_url: str | None = None) -> dict[str, Any]:
    """Authenticate and return a session dict {client, token, baseUrl}."""
    url = _base_url(base_url)
    client = _open_client(url)
    try:
        resp = client.post("/token", json={"address": address, "password": password})
        resp.raise_for_status()
        token = resp.json().get("token")
        if not token:
            raise RuntimeError("Mail.tm token missing in response")
    except Exception:
        client.close()
        raise
    client.headers["Authorization"] = f"Bearer {token}"
    return {"client": client, "token": token, "baseUrl": url, "address": address}


def disconnect(session: dict[str, Any]) -> None:
    session["client"].close()


def create_random_account(base_url: str | None = None) -> dict[str, Any]:
    """Create a fresh mail.tm account with a random local part + password.

    Fails fast when the account cannot be logged into — better to error
    here than persist a dead profile.
    """
    url = _base_url(base_url)
    client = _open_client(url)
    try:
        resp = client.get("/domains")
        resp.raise_for_status()
        domains = _hydra_list(resp.json())
        if not domains:
            raise RuntimeError("Mail.tm returned no available domains")
        domain = next(
            (d.get("domain") for d in domains if d.get("isActive", True) and d.get("domain")),
            domains[0].get("domain"),
        )
        if not domain:
            raise RuntimeError("Mail.tm returned no usable domain")

        local = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        address = f"{local}@{domain}"
        password = "".join(random.choices(string.ascii_letters + string.digits, k=16))

        resp = client.post("/accounts", json={"address": address, "password": password})
        resp.raise_for_status()
        resp = client.post("/token", json={"address": address, "password": password})
        resp.raise_for_status()
        if not resp.json().get("token"):
            raise RuntimeError("Mail.tm login failed for the fresh account")
        return {"address": address, "password": password, "baseUrl": url}
    finally:
        client.close()


def list_messages(
    session: dict[str, Any], query: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """List messages (newest first), fetching full bodies per item."""
    query = query or {}
    limit = query.get("limit")
    client: httpx.Client = session["client"]
    resp = client.get("/messages")
    resp.raise_for_status()
    items = _hydra_list(resp.json())
    if limit:
        items = items[: int(limit)]

    messages: list[dict[str, Any]] = []
    for item in items:
        if not _matches(item, query):
            continue
        try:
            full = client.get(f"/messages/{item['id']}")
            full.raise_for_status()
            messages.append(_to_email_message(full.json(), item))
        except Exception:  # noqa: BLE001
            pass
    return messages


def get_message(session: dict[str, Any], message_id: str) -> dict[str, Any] | None:
    client: httpx.Client = session["client"]
    try:
        resp = client.get(f"/messages/{message_id}")
        resp.raise_for_status()
    except Exception:  # noqa: BLE001
        return None
    full = resp.json()
    return _to_email_message(full, full)


def mark_seen(session: dict[str, Any], message_id: str) -> None:
    client: httpx.Client = session["client"]
    resp = client.patch(f"/messages/{message_id}", json={"seen": True})
    resp.raise_for_status()


def delete_message(session: dict[str, Any], message_id: str) -> None:
    client: httpx.Client = session["client"]
    resp = client.delete(f"/messages/{message_id}")
    resp.raise_for_status()


def _hydra_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        member = data.get("hydra:member", [])
        return member if isinstance(member, list) else []
    return []


def _matches(item: dict[str, Any], query: dict[str, Any]) -> bool:
    def _contains(hay: str, needle: str | None) -> bool:
        return not needle or needle.lower() in hay.lower()

    from_addr = ((item.get("from") or {}).get("address")) or ""
    subject = item.get("subject") or ""
    intro = item.get("intro") or ""

    if query.get("unreadOnly") and item.get("seen"):
        return False
    if not _contains(from_addr, query.get("from")):
        return False
    if not _contains(subject, query.get("subjectContains")):
        return False
    search = query.get("search")
    if search and not (
        _contains(subject, search) or _contains(from_addr, search) or _contains(intro, search)
    ):
        return False
    body = query.get("bodyContains")
    if body and not (_contains(intro, body) or _contains(subject, body)):
        return False
    return True


def _addr(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value or {}
    return {"name": value.get("name") or None, "email": value.get("address") or ""}


def _to_email_message(full: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    html_parts = full.get("html")
    html = "\n".join(html_parts) if isinstance(html_parts, list) else (html_parts or None)
    message_id = str(full.get("id") or fallback.get("id") or "")
    return {
        "id": message_id,
        "providerMessageId": full.get("msgid") or fallback.get("msgid") or message_id,
        "from": _addr(full.get("from") or fallback.get("from")),
        "to": [_addr(a) for a in (full.get("to") or fallback.get("to") or [])],
        "cc": [_addr(a) for a in (full.get("cc") or [])],
        "bcc": [_addr(a) for a in (full.get("bcc") or [])],
        "subject": full.get("subject") or fallback.get("subject") or "",
        "text": full.get("text"),
        "html": html,
        "headers": {},
        "attachments": [],
        "isRead": bool(full.get("seen", fallback.get("seen", False))),
        "receivedAt": full.get("createdAt") or fallback.get("createdAt") or "",
    }
