"""Protocol tests — drive the plugin over raw stdin/stdout.

Spawns ``python -m stitch_mail`` (no host dependency) and walks the
JSON-RPC 2.0 line protocol.  IMAP commands run against the in-process
fake IMAP socket server (``fake_imap.FakeImapServer``) so the plugin's
real ``imaplib`` transport is exercised end to end; mail.tm commands run
against ``fake_mailtm.FakeMailTmServer`` via the
``STITCH_MAIL_MAILTM_URL`` env override.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from tests.fake_imap import FakeImapServer
from tests.fake_mailtm import FakeMailTmServer

MODULE = "stitch_mail"
PLUGIN_ID = "stitch-mail"
TEST_COMMAND = "list_profiles"

PACKAGE_DIR = Path(__file__).resolve().parents[1]

_IMAP_CONNECT_INPUT = {
    "provider": "imap",
    "accountId": "imap:user@example.com",
    "credentials": {
        "type": "imap",
        "value": {
            "host": "imap.gmail.com",
            "port": 993,
            "username": "user@example.com",
            "password": "secret",
            "useTls": True,
        },
    },
    "options": {"mailbox": "INBOX", "readOnly": True},
}

_MAIL_TM_CONNECT_INPUT = {
    "provider": "mail_tm",
    "accountId": "mailtm:tmp@example.com",
    "credentials": {
        "type": "mail_tm",
        "value": {
            "address": "tmp@example.com",
            "password": "pw",
            "baseUrl": "https://api.mail.tm",
        },
    },
    "options": None,
}


def _request(rid: int, method: str, params: dict | None = None) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    )


def _init_params(td: str) -> dict:
    return {
        "engine_api": 2,
        "plugin_id": PLUGIN_ID,
        "db_path": str(Path(td) / "plugin.db"),
        "data_dir": td,
        "supported": [],
    }


def _drive(lines: list[str]) -> dict[int, dict]:
    """Feed JSON-RPC request lines, return responses keyed by id."""
    proc = subprocess.run(
        [sys.executable, "-m", MODULE],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(PACKAGE_DIR),
        timeout=60,
    )
    assert proc.returncode == 0, f"plugin exited {proc.returncode}: {proc.stderr}"
    responses: dict[int, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            obj = json.loads(line)
            responses[obj["id"]] = obj
    return responses


class _InteractivePlugin:
    """Long-lived plugin process: write one request, read one response."""

    def __init__(self, td: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", MODULE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=str(PACKAGE_DIR),
        )
        self._rid = 0
        self.init(td)
        self.call("_migrate_db", {"from_version": 0, "to_version": 1})

    def init(self, td: str) -> dict[str, Any]:
        return self._roundtrip("plugin.init", _init_params(td))

    def call(self, name: str, params: dict | None = None) -> Any:
        return self._roundtrip("plugin.call", {"name": name, "params": params or {}})

    def _roundtrip(self, method: str, params: dict) -> Any:
        self._rid += 1
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(_request(self._rid, method, params) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, "plugin closed stdout before responding"
        response = json.loads(line)
        assert "result" in response, f"{method} failed: {response.get('error')}"
        return response["result"]

    def close(self) -> None:
        self._rid += 1
        assert self.proc.stdin is not None
        self.proc.stdin.write(_request(self._rid, "plugin.shutdown") + "\n")
        self.proc.stdin.flush()
        try:
            self.proc.wait(timeout=10)
        finally:
            self.proc.kill()
            self.proc.stdin.close()
            self.proc.stdout.close()
            self.proc.stderr.close()


def _fake_imap_input(host: str, port: int, username: str = "user@example.com") -> dict:
    return {
        "provider": "imap",
        "accountId": f"imap:{username}",
        "credentials": {
            "type": "imap",
            "value": {
                "host": host,
                "port": port,
                "username": username,
                "password": "secret",
                "useTls": False,
            },
        },
        "options": {"mailbox": "INBOX", "readOnly": True},
    }


def test_lifecycle_init_migrate_ping_command_shutdown() -> None:
    """Full lifecycle over raw stdin with a temp db: handshake, migration,
    liveness, one readonly command, and graceful shutdown."""
    with tempfile.TemporaryDirectory() as td:
        responses = _drive(
            [
                _request(1, "plugin.init", _init_params(td)),
                _request(
                    2,
                    "plugin.call",
                    {
                        "name": "_migrate_db",
                        "params": {"from_version": 0, "to_version": 1},
                    },
                ),
                _request(3, "plugin.ping"),
                _request(4, "plugin.call", {"name": TEST_COMMAND, "params": {}}),
                _request(5, "plugin.shutdown"),
            ]
        )

    init = responses[1]["result"]
    assert init["plugin_id"] == PLUGIN_ID
    assert "capabilities" in init

    assert responses[3]["result"] == "pong"

    command_response = responses[4]
    assert "result" in command_response, (
        f"{TEST_COMMAND} returned an error: {command_response.get('error')}"
    )
    assert command_response["result"] == []

    assert responses[5]["result"] is None


def test_upsert_list_profiles_contract_roundtrip() -> None:
    """Upsert via the frontend shape, then list returns the full camelCase
    contract row: connectInput round-trips losslessly and mine/shared are
    computed from the caller uid.  A fresh DB lists []."""
    with tempfile.TemporaryDirectory() as td:
        responses = _drive(
            [
                _request(1, "plugin.init", _init_params(td)),
                _request(
                    2,
                    "plugin.call",
                    {"name": "_migrate_db", "params": {"from_version": 0, "to_version": 1}},
                ),
                _request(
                    3, "plugin.call", {"name": "list_profiles", "params": {"caller_user_id": 7}}
                ),
                _request(
                    4,
                    "plugin.call",
                    {
                        "name": "upsert_profile",
                        "params": {
                            "input": {
                                "label": "Gmail main",
                                "connectInput": _IMAP_CONNECT_INPUT,
                            },
                            "caller_user_id": 7,
                        },
                    },
                ),
                _request(
                    5, "plugin.call", {"name": "list_profiles", "params": {"caller_user_id": 7}}
                ),
                _request(6, "plugin.shutdown"),
            ]
        )

    assert responses[3]["result"] == []

    upserted = responses[4]["result"]
    assert upserted["label"] == "Gmail main"
    assert upserted["provider"] == "imap"
    assert upserted["accountId"] == "imap:user@example.com"
    assert upserted["connectInput"] == _IMAP_CONNECT_INPUT
    assert upserted["createdAt"]
    assert upserted["updatedAt"]
    assert upserted["mine"] is True
    assert upserted["shared"] is False

    assert responses[5]["result"] == [upserted]


def test_upsert_list_profiles_mailtm_guest_shared() -> None:
    """A guest upsert (no caller uid) creates a shared row: list as guest
    returns it with shared=True / mine=False and the mail_tm credentials
    round-trip losslessly."""
    with tempfile.TemporaryDirectory() as td:
        responses = _drive(
            [
                _request(1, "plugin.init", _init_params(td)),
                _request(
                    2,
                    "plugin.call",
                    {"name": "_migrate_db", "params": {"from_version": 0, "to_version": 1}},
                ),
                _request(
                    3,
                    "plugin.call",
                    {
                        "name": "upsert_profile",
                        "params": {
                            "input": {
                                "label": "Mail.tm temp",
                                "connectInput": _MAIL_TM_CONNECT_INPUT,
                            }
                        },
                    },
                ),
                _request(4, "plugin.call", {"name": "list_profiles", "params": {}}),
                _request(5, "plugin.shutdown"),
            ]
        )

    upserted = responses[3]["result"]
    assert upserted["provider"] == "mail_tm"
    assert upserted["accountId"] == "mailtm:tmp@example.com"
    assert upserted["connectInput"] == _MAIL_TM_CONNECT_INPUT
    assert upserted["mine"] is False
    assert upserted["shared"] is True

    assert responses[4]["result"] == [upserted]


def test_get_provider_catalog_shapes() -> None:
    """get_provider_catalog lists the imap and mail_tm providers with the
    full capability shape — the frontend DTO contract the dual-routed
    command must uphold."""
    with tempfile.TemporaryDirectory() as td:
        responses = _drive(
            [
                _request(1, "plugin.init", _init_params(td)),
                _request(2, "plugin.call", {"name": "get_provider_catalog", "params": {}}),
                _request(3, "plugin.shutdown"),
            ]
        )

    catalog = responses[2]["result"]
    assert catalog, catalog
    capability_fields = {
        "canDelete", "canMarkAsRead", "canSearchBody",
        "canDownloadAttachments", "canListFolders",
    }
    providers = {item["provider"] for item in catalog}
    assert providers == {"imap", "mail_tm"}, catalog
    for item in catalog:
        assert {
            "provider", "displayName", "available",
            "capabilities", "supportsProfileConnect",
        } <= set(item), item
        assert capability_fields <= set(item["capabilities"]), item


def test_migrate_backfills_legacy_profile_rows() -> None:
    """Legacy rows (email/provider/credentials_json only) gain
    label/account_id/connect_input_json on migrate and list as valid
    contract rows; unparsable credentials_json degrades to an empty
    value.  migrate() is idempotent."""
    import sqlite3

    if str(PACKAGE_DIR) not in sys.path:
        sys.path.insert(0, str(PACKAGE_DIR))
    from stitch_mail import storage

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "legacy.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE mail_profiles ("
            "id TEXT PRIMARY KEY, "
            "email TEXT NOT NULL DEFAULT '', "
            "provider TEXT NOT NULL DEFAULT 'imap', "
            "credentials_json TEXT NOT NULL DEFAULT '{}', "
            "owner_id INTEGER, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute(
            "INSERT INTO mail_profiles (id, email, provider, credentials_json) "
            "VALUES ('legacy-1', 'a@x.com', 'imap', ?)",
            (json.dumps({"host": "h", "username": "a@x.com", "password": "p"}),),
        )
        conn.execute(
            "INSERT INTO mail_profiles (id, email, provider, credentials_json) "
            "VALUES ('legacy-2', 'b@x.com', 'mail_tm', 'not-json')"
        )
        conn.commit()
        conn.close()

        storage.migrate(db)
        storage.migrate(db)

        profiles = storage.list_profiles(db)
        by_id = {p["id"]: p for p in profiles}
        assert set(by_id) == {"legacy-1", "legacy-2"}

        first = by_id["legacy-1"]
        assert first["label"] == "a@x.com"
        assert first["accountId"] == "imap:a@x.com"
        assert first["connectInput"] == {
            "provider": "imap",
            "accountId": "imap:a@x.com",
            "credentials": {
                "type": "imap",
                "value": {"host": "h", "username": "a@x.com", "password": "p"},
            },
            "options": None,
        }
        assert first["mine"] is False
        assert first["shared"] is True

        assert by_id["legacy-2"]["connectInput"]["credentials"]["value"] == {}


# ── Real IMAP session commands against the fake IMAP server ─────────────────


def test_connect_list_mark_read_delete_against_fake_imap() -> None:
    """Given the fake IMAP server holding one message, when connect →
    list → mark_as_read → delete → list run over the RPC protocol, then
    the session descriptor matches EmailMailboxSession, the message
    carries the full EmailMessage shape, mark_as_read flags it on the
    server, and delete expunges it."""
    fake = FakeImapServer()
    fake.add_message("Verify your account", "Your code is 654321.", to="user@example.com")
    host, port = fake.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            plugin = _InteractivePlugin(td)
            try:
                session = plugin.call(
                    "connect", {"input": _fake_imap_input(host, port)}
                )
                assert {
                    "sessionId", "provider", "accountId",
                    "capabilities", "connectedAt",
                } <= set(session), session
                assert session["provider"] == "imap"
                assert session["accountId"] == "imap:user@example.com"
                assert session["capabilities"]["canListFolders"] is True

                messages = plugin.call("list", {"sessionId": session["sessionId"]})
                assert len(messages) == 1, messages
                message = messages[0]
                assert {
                    "id", "providerMessageId", "from", "to", "cc", "bcc",
                    "subject", "text", "html", "headers", "attachments",
                    "isRead", "receivedAt",
                } <= set(message), message
                assert message["subject"] == "Verify your account"
                assert message["text"] == "Your code is 654321."
                assert message["from"]["email"] == "sender@example.com"

                marked = plugin.call(
                    "mark_as_read",
                    {"sessionId": session["sessionId"], "messageId": message["id"]},
                )
                assert marked == {"success": True}
                assert fake.seen == {1}, fake.seen

                deleted = plugin.call(
                    "delete",
                    {"sessionId": session["sessionId"], "messageId": message["id"]},
                )
                assert deleted == {"success": True}

                remaining = plugin.call("list", {"sessionId": session["sessionId"]})
                assert remaining == [], remaining

                folders = plugin.call(
                    "list_folders", {"sessionId": session["sessionId"]}
                )
                paths = {folder["path"] for folder in folders}
                assert "INBOX" in paths, folders
                for folder in folders:
                    assert {"id", "path", "name", "kind", "delimiter"} <= set(folder)

                plugin.call("disconnect", {"sessionId": session["sessionId"]})
            finally:
                plugin.close()
    finally:
        fake.stop()

    assert fake.logins == [("user@example.com", "secret")], fake.logins


def test_wait_for_email_polls_until_message_arrives() -> None:
    """Given an empty fake mailbox and a message injected mid-poll, when
    wait_for_email runs with a short poll interval, then it returns the
    arrived message (real poll loop, not a stub)."""
    fake = FakeImapServer()
    host, port = fake.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            plugin = _InteractivePlugin(td)
            injector: threading.Timer | None = None
            try:
                session = plugin.call(
                    "connect", {"input": _fake_imap_input(host, port)}
                )
                injector = threading.Timer(
                    0.5,
                    lambda: fake.add_message("Welcome", "Thanks for signing up."),
                )
                injector.start()
                started = time.monotonic()
                message = plugin.call(
                    "wait_for_email",
                    {
                        "sessionId": session["sessionId"],
                        "query": {},
                        "options": {"timeoutMs": 20000, "pollIntervalMs": 200},
                    },
                )
                elapsed = time.monotonic() - started
                assert message["subject"] == "Welcome", message
                assert elapsed >= 0.4, elapsed
            finally:
                if injector is not None:
                    injector.cancel()
                plugin.close()
    finally:
        fake.stop()


def test_wait_otp_returns_code_from_fake_imap() -> None:
    """Given a stored IMAP profile pointing at the fake server with a
    verification message, when wait_otp runs for the profile's address,
    then the real IMAP poll extracts the 6-digit code."""
    fake = FakeImapServer()
    fake.add_message("Verify your account", "Your code is 654321.", to="user@example.com")
    host, port = fake.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            plugin = _InteractivePlugin(td)
            try:
                plugin.call(
                    "upsert_profile",
                    {
                        "input": {
                            "label": "Fake IMAP",
                            "connectInput": _fake_imap_input(host, port),
                        }
                    },
                )
                code = plugin.call(
                    "wait_otp",
                    {
                        "email": "user@example.com",
                        "subject_filter": "Verify",
                        "timeout": 6,
                        "poll_interval_s": 0.2,
                    },
                )
                assert code == "654321"
            finally:
                plugin.close()
    finally:
        fake.stop()


def test_wait_otp_timeout_returns_empty_string() -> None:
    """Given a stored IMAP profile whose mailbox holds no code, when
    wait_otp runs with a short timeout, then it returns '' (genuine
    timeout) instead of raising or blocking past the host kill."""
    fake = FakeImapServer()
    fake.add_message("No code here", "plain body without digits", to="user@example.com")
    host, port = fake.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            plugin = _InteractivePlugin(td)
            try:
                plugin.call(
                    "upsert_profile",
                    {
                        "input": {
                            "label": "Fake IMAP",
                            "connectInput": _fake_imap_input(host, port),
                        }
                    },
                )
                code = plugin.call(
                    "wait_otp",
                    {
                        "email": "user@example.com",
                        "timeout": 1,
                        "poll_interval_s": 0.3,
                    },
                )
                assert code == ""
            finally:
                plugin.close()
    finally:
        fake.stop()


def test_wait_otp_without_profile_errors() -> None:
    """Given no stored mailbox profile, when wait_otp runs, then the
    handler raises (JSON-RPC error) so the host SPI bridge falls back to
    the built-in EmailService."""
    with tempfile.TemporaryDirectory() as td:
        responses = _drive(
            [
                _request(1, "plugin.init", _init_params(td)),
                _request(
                    2,
                    "plugin.call",
                    {"name": "_migrate_db", "params": {"from_version": 0, "to_version": 1}},
                ),
                _request(
                    3,
                    "plugin.call",
                    {"name": "wait_otp", "params": {"email": "nobody@example.com"}},
                ),
                _request(4, "plugin.shutdown"),
            ]
        )

    assert "error" in responses[3], responses[3]


def test_generate_from_settings_real_derivation() -> None:
    """generate_from_settings derives real addresses: single falls back
    to the default address, plus_alias tags the base, catch_all builds a
    random local part on the given domain, and a stored profile provides
    the base address when the caller sends none."""
    with tempfile.TemporaryDirectory() as td:
        responses = _drive(
            [
                _request(1, "plugin.init", _init_params(td)),
                _request(
                    2,
                    "plugin.call",
                    {"name": "_migrate_db", "params": {"from_version": 0, "to_version": 1}},
                ),
                _request(
                    3,
                    "plugin.call",
                    {"name": "generate_from_settings", "params": {}},
                ),
                _request(
                    4,
                    "plugin.call",
                    {
                        "name": "generate_from_settings",
                        "params": {"strategy": "plus_alias", "imapUser": "base@gmail.com"},
                    },
                ),
                _request(
                    5,
                    "plugin.call",
                    {
                        "name": "generate_from_settings",
                        "params": {"strategy": "catch_all", "domain": "mydomain.ru"},
                    },
                ),
                _request(
                    6,
                    "plugin.call",
                    {
                        "name": "upsert_profile",
                        "params": {
                            "input": {
                                "label": "Base",
                                "connectInput": _fake_imap_input("h", 993, "stored@x.com"),
                            }
                        },
                    },
                ),
                _request(
                    7,
                    "plugin.call",
                    {"name": "generate_from_settings", "params": {}},
                ),
                _request(8, "plugin.shutdown"),
            ]
        )

    single = responses[3]["result"]
    assert single == {"email": "user@example.com", "strategy": "single"}, single

    plus = responses[4]["result"]
    assert plus["strategy"] == "plus_alias"
    assert plus["email"].startswith("base+"), plus
    assert plus["email"].endswith("@gmail.com"), plus

    catch_all = responses[5]["result"]
    assert catch_all["strategy"] == "catch_all"
    assert catch_all["email"].endswith("@mydomain.ru"), catch_all

    from_profile = responses[7]["result"]
    assert from_profile == {"email": "stored@x.com", "strategy": "single"}, from_profile


def test_test_strategies_shapes() -> None:
    """test_strategies returns the five strategy probes with derived
    addresses, mirroring the removed core command."""
    with tempfile.TemporaryDirectory() as td:
        responses = _drive(
            [
                _request(1, "plugin.init", _init_params(td)),
                _request(2, "plugin.call", {"name": "test_strategies", "params": {}}),
                _request(3, "plugin.shutdown"),
            ]
        )

    rows = responses[2]["result"]
    strategies = [row[0] for row in rows]
    assert strategies == [
        "single", "plus_alias", "catch_all", "gmail_dot_alias", "pool",
    ], strategies
    by_name = dict(rows)
    assert by_name["single"]["email"] == "test.user@gmail.com"
    assert "+" in by_name["plus_alias"]["email"]
    assert by_name["catch_all"]["email"].endswith("@mydomain.ru")
    gmail_local = by_name["gmail_dot_alias"]["email"].rsplit("@", 1)[0]
    assert gmail_local.replace(".", "") == "testuser", by_name["gmail_dot_alias"]
    assert by_name["pool"]["email"] in ("user1@mail.ru", "user2@mail.ru")


def test_create_mailtm_account_against_fake(monkeypatch) -> None:
    """Given the fake mail.tm server wired via STITCH_MAIL_MAILTM_URL,
    when create_mailtm_account runs, then a random account is created
    (domains → account → login) and the credentials shape matches the
    frontend MailTmAccountCredentials contract."""
    fake = FakeMailTmServer()
    base_url = fake.start()
    monkeypatch.setenv("STITCH_MAIL_MAILTM_URL", base_url)
    try:
        with tempfile.TemporaryDirectory() as td:
            responses = _drive(
                [
                    _request(1, "plugin.init", _init_params(td)),
                    _request(2, "plugin.call", {"name": "create_mailtm_account", "params": {}}),
                    _request(3, "plugin.shutdown"),
                ]
            )
    finally:
        fake.stop()

    assert "result" in responses[2], responses[2].get("error")
    result = responses[2]["result"]
    assert set(result) == {"address", "password", "baseUrl"}, result
    assert result["address"].endswith("@fake.tm"), result
    assert result["password"]
    assert result["baseUrl"] == base_url
    assert fake.created == [
        {"address": result["address"], "password": result["password"]}
    ], fake.created


def test_mailtm_session_list_against_fake(monkeypatch) -> None:
    """Given the fake mail.tm server, when a mail_tm session connects and
    lists messages, then the REST-backed session returns the message in
    the EmailMessage shape."""
    fake = FakeMailTmServer()
    fake.add_message("Hello", "mail.tm body text")
    base_url = fake.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            plugin = _InteractivePlugin(td)
            try:
                session = plugin.call(
                    "connect",
                    {
                        "input": {
                            "provider": "mail_tm",
                            "accountId": "mailtm:box@fake.tm",
                            "credentials": {
                                "type": "mail_tm",
                                "value": {
                                    "address": "box@fake.tm",
                                    "password": "pw",
                                    "baseUrl": base_url,
                                },
                            },
                            "options": None,
                        }
                    },
                )
                assert session["provider"] == "mail_tm"
                assert session["capabilities"]["canListFolders"] is False

                messages = plugin.call("list", {"sessionId": session["sessionId"]})
                assert len(messages) == 1, messages
                assert messages[0]["subject"] == "Hello"
                assert messages[0]["text"] == "mail.tm body text"
            finally:
                plugin.close()
    finally:
        fake.stop()
