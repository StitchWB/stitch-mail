"""Protocol smoke test — drives the plugin over raw stdin/stdout.

Spawns ``python -m stitch_mail`` (no host dependency) and walks the
JSON-RPC 2.0 line protocol: init → _migrate_db → ping → list_profiles → shutdown.
Mirrors the starter test in the plugin template repo.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

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
        timeout=30,
    )
    assert proc.returncode == 0, f"plugin exited {proc.returncode}: {proc.stderr}"
    responses: dict[int, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            obj = json.loads(line)
            responses[obj["id"]] = obj
    return responses


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


def test_session_folder_catalog_stub_shapes() -> None:
    """connect / connect_profile return the full EmailMailboxSession shape,
    list_folders the full EmailFolder shape, and get_provider_catalog items
    carry full capabilities — the frontend DTO contracts the dual-routed
    commands must uphold."""
    with tempfile.TemporaryDirectory() as td:
        responses = _drive(
            [
                _request(1, "plugin.init", _init_params(td)),
                _request(2, "plugin.call", {"name": "connect", "params": {}}),
                _request(3, "plugin.call", {"name": "connect_profile", "params": {}}),
                _request(4, "plugin.call", {"name": "list_folders", "params": {}}),
                _request(5, "plugin.call", {"name": "get_provider_catalog", "params": {}}),
                _request(6, "plugin.shutdown"),
            ]
        )

    session_fields = {
        "sessionId", "provider", "accountId", "capabilities", "connectedAt",
    }
    capability_fields = {
        "canDelete", "canMarkAsRead", "canSearchBody",
        "canDownloadAttachments", "canListFolders",
    }
    for rid in (2, 3):
        result = responses[rid]["result"]
        assert session_fields <= set(result), result
        assert capability_fields <= set(result["capabilities"]), result

    folders = responses[4]["result"]
    assert folders, folders
    for folder in folders:
        assert {"id", "path", "name", "kind", "delimiter"} <= set(folder), folder

    catalog = responses[5]["result"]
    assert catalog, catalog
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
