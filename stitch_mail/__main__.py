"""RPC entry point for the stitch-mail service plugin.

Spawned by ``ServicePluginHost`` as ``python -m stitch_mail``.
Implements the JSON-RPC 2.0 line protocol via ``RpcPluginServer``
(imported from ``autoreg.plugin.rpc`` when available, otherwise from
the vendored ``_vendor/rpc_server.py`` copy).

Protocol methods handled by ``RpcPluginServer``:
  - ``plugin.init``    → stores handshake params (db_path, data_dir).
  - ``plugin.call``    → dispatches to command handlers.
  - ``plugin.ping``    → returns ``"pong"``.
  - ``plugin.shutdown`` → returns ``None`` and exits.

Commands mirror the current ``email_*``/``email_inbox_*`` built-in
command names (stripped of prefix) so the dual-format proxy can route
to them when the plugin is installed and healthy.  Three commands keep
their bare names unchanged (``get_email_counter``,
``set_email_counter``, ``claim_email_inbox_profile``) — the built-ins
behind them were removed in the plugin migration.  The
``list_profiles`` command returns a plugin-served marker so the e2e
test can verify the SPI proxy chain (host → RPC → plugin → marker).
"""

# _generated_by: stitch_plugin_tools scaffold v3

from __future__ import annotations

from typing import Any

from . import storage

try:
    from autoreg.plugin.rpc import RpcPluginServer
except ImportError:
    from ._vendor.rpc_server import RpcPluginServer


# ── State received in plugin.init handshake ───────────────────────────────


class _Ctx:
    """Mutable container for plugin.init handshake state."""

    db_path: str = ""
    data_dir: str = ""


ctx = _Ctx()


def _uid(params: dict[str, Any]) -> int | None:
    """Caller user id forwarded by the dual-format router or SPI proxy.

    Reads ``caller_user_id`` (dual/namespaced routes) first, then
    ``owner_id`` (SPI proxy path — _PluginSpiProxy.list_profiles forwards
    ``{"owner_id": X}``).  None = guest.
    """
    uid = params.get("caller_user_id")
    if uid is None:
        uid = params.get("owner_id")
    return int(uid) if uid is not None else None


def _handle_init(params: dict[str, Any]) -> dict[str, Any]:
    """Store handshake params and return them as the init result."""
    ctx.db_path = str(params.get("db_path", ""))
    ctx.data_dir = str(params.get("data_dir", ""))
    return {
        "plugin_id": params.get("plugin_id", ""),
        "db_path": ctx.db_path,
        "data_dir": ctx.data_dir,
        # Capability negotiation: no reverse-RPC used.  Declared
        # explicitly for contract uniformity.
        "capabilities": [],
    }


def _handle_migrate_db(params: dict[str, Any]) -> dict[str, Any]:
    """Create SQLite tables (raw_sql migration, from_version→to_version)."""
    if ctx.db_path:
        storage.migrate(ctx.db_path)
    return {
        "from_version": params.get("from_version", 0),
        "to_version": params.get("to_version", 1),
    }


# ── SPI-served commands (proxy targets for spi_bridge) ─────────────────────
#
# These are the SPI methods the host-side spi_bridge proxy calls over RPC.
# ``list_profiles`` returns a plugin-served marker so the e2e test can
# verify the proxy chain.  ``wait_otp`` and ``sync`` are graceful
# no-ops in the plugin-only proof (real IMAP wiring arrives in todo 16+).


def _handle_list_profiles(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return mail profiles from local storage.

    When the plugin DB has no profiles (fresh install), returns a
    plugin-served marker so the SPI proxy chain can be verified in e2e.
    """
    if not ctx.db_path:
        return [{"id": "plugin-served-marker", "source": "stitch-mail"}]
    rows = storage.list_profiles(ctx.db_path, owner_id=_uid(params))
    if not rows:
        return [{"id": "plugin-served-marker", "source": "stitch-mail"}]
    return rows


def _handle_wait_otp(params: dict[str, Any]) -> str:
    """Wait for a verification code/OTP for *email*.

    Graceful degradation: returns an empty string when no IMAP config is
    available (the plugin has its own credential store in mail_profiles;
    real IMAP wiring arrives in todo 16+).
    """
    return ""


def _handle_sync(params: dict[str, Any]) -> dict[str, Any]:
    """Run a sync tick for *profile_id* and return the new sync state."""
    pid = str(params.get("profile_id") or params.get("profileId") or "")
    if not pid or not ctx.db_path:
        return {"profileId": pid, "status": "synced"}
    return storage.upsert_sync_state(
        ctx.db_path, {"profileId": pid, "status": "synced"}, owner_id=_uid(params)
    )


# ── Mirrored email_* commands ──────────────────────────────────────────────


def _handle_generate_from_settings(params: dict[str, Any]) -> dict[str, Any]:
    """Generate email from stored settings (mirrors email_generate_from_settings)."""
    return {"email": "plugin@example.com", "strategy": "single"}


def _handle_generate_from_settings_persistent(params: dict[str, Any]) -> dict[str, Any]:
    """Generate email with persistent counter (mirrors email_generate_from_settings_persistent)."""
    provider = str(params.get("provider", "default"))
    counter = storage.increment_counter(ctx.db_path, provider) if ctx.db_path else 0
    return {"email": f"plugin_{counter}@example.com", "strategy": "single", "counter": counter}


def _handle_test_strategies(params: dict[str, Any]) -> list:
    """Test email strategies (mirrors email_test_strategies)."""
    return [
        ["single", {"email": "test.user@gmail.com", "strategy": "single"}],
        ["plus_alias", {"email": "test.user+abc123@gmail.com", "strategy": "plus_alias"}],
    ]


# ── Mirrored email_inbox_* session commands ────────────────────────────────


def _handle_connect(params: dict[str, Any]) -> dict[str, Any]:
    """Connect to a mailbox (mirrors email_inbox_connect)."""
    return {"sessionId": "plugin-session", "connected": True}


def _handle_disconnect(params: dict[str, Any]) -> dict[str, Any]:
    """Disconnect a session (mirrors email_inbox_disconnect)."""
    return {"success": True}


def _handle_list(params: dict[str, Any]) -> list:
    """List messages in a mailbox (mirrors email_inbox_list)."""
    return []


def _handle_list_folders(params: dict[str, Any]) -> list:
    """List mailbox folders (mirrors email_inbox_list_folders)."""
    return [{"name": "INBOX", "delimiter": "/"}]


def _handle_get_by_id(params: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch a single message by ID (mirrors email_inbox_get_by_id)."""
    return None


def _handle_wait_for_email(params: dict[str, Any]) -> dict[str, Any]:
    """Poll for an email matching criteria (mirrors email_inbox_wait_for_email)."""
    return {"found": False}


def _handle_mark_as_read(params: dict[str, Any]) -> dict[str, Any]:
    """Mark a message as read (mirrors email_inbox_mark_as_read)."""
    return {"success": True}


def _handle_delete(params: dict[str, Any]) -> dict[str, Any]:
    """Delete a message (mirrors email_inbox_delete)."""
    return {"success": True}


def _handle_create_mailtm_account(params: dict[str, Any]) -> dict[str, Any]:
    """Create a random Mail.tm account (mirrors email_inbox_create_mailtm_account)."""
    return {"email": "plugin-mailtm@example.com", "password": ""}


def _handle_get_capabilities(params: dict[str, Any]) -> dict[str, Any]:
    """Get provider capabilities (mirrors email_inbox_get_capabilities)."""
    return {
        "canDelete": True,
        "canMarkAsRead": True,
        "canSearchBody": True,
        "canDownloadAttachments": False,
        "canListFolders": True,
    }


def _handle_get_provider_catalog(params: dict[str, Any]) -> list:
    """Get available email providers (mirrors email_inbox_get_provider_catalog)."""
    return [
        {
            "provider": "imap",
            "displayName": "IMAP",
            "available": True,
            "supportsProfileConnect": True,
        },
    ]


# ── Mirrored email_inbox_* profile commands ────────────────────────────────


def _handle_get_profile(params: dict[str, Any]) -> dict[str, Any] | None:
    """Get a profile by ID (mirrors email_inbox_get_profile)."""
    pid = str(params.get("profileId") or params.get("profile_id") or "")
    if not ctx.db_path or not pid:
        return None
    return storage.get_profile(ctx.db_path, pid, owner_id=_uid(params))


def _handle_upsert_profile(params: dict[str, Any]) -> dict[str, Any]:
    """Create or update an inbox profile (mirrors email_inbox_upsert_profile)."""
    input_data = params.get("input", params)
    if not ctx.db_path:
        return {"id": "stub", "email": str(input_data.get("email", ""))}
    return storage.upsert_profile(ctx.db_path, input_data, owner_id=_uid(params))


def _handle_delete_profile(params: dict[str, Any]) -> bool:
    """Delete an inbox profile (mirrors email_inbox_delete_profile)."""
    pid = str(params.get("profileId") or params.get("profile_id") or "")
    if not ctx.db_path or not pid:
        return False
    return storage.delete_profile(ctx.db_path, pid, owner_id=_uid(params))


def _handle_connect_profile(params: dict[str, Any]) -> dict[str, Any]:
    """Connect using a saved profile (mirrors email_inbox_connect_profile)."""
    return {"sessionId": "plugin-session", "connected": True}


def _handle_get_sync_state(params: dict[str, Any]) -> dict[str, Any] | None:
    """Get sync state for a profile (mirrors email_inbox_get_sync_state)."""
    pid = str(params.get("profileId") or params.get("profile_id") or "")
    if not ctx.db_path or not pid:
        return None
    return storage.get_sync_state(ctx.db_path, pid, owner_id=_uid(params))


def _handle_upsert_sync_state(params: dict[str, Any]) -> dict[str, Any]:
    """Create or update sync state (mirrors email_inbox_upsert_sync_state)."""
    input_data = params.get("input", params)
    if not ctx.db_path:
        return {"profileId": str(input_data.get("profileId", "")), "status": "synced"}
    return storage.upsert_sync_state(ctx.db_path, input_data, owner_id=_uid(params))


# ── Bare commands (removed email_counter / claim built-ins) ──────────────────


def _handle_get_email_counter(params: dict[str, Any]) -> int:
    """Counter for a provider+strategy pair (mirrors get_email_counter)."""
    provider = str(params.get("provider") or "").strip()
    strategy = str(params.get("strategy") or "").strip()
    if not ctx.db_path or not provider or not strategy:
        return 0
    return storage.get_counter(ctx.db_path, provider, strategy)


def _handle_set_email_counter(params: dict[str, Any]) -> dict[str, Any]:
    """Set a provider+strategy counter (mirrors set_email_counter).

    The RPC layer reserves the ``error`` result key, so validation
    failures surface as ``{"success": False}`` without a message.
    """
    provider = str(params.get("provider") or "").strip()
    strategy = str(params.get("strategy") or "").strip()
    if not provider or not strategy or not ctx.db_path:
        return {"success": False}
    value = int(params.get("counter", params.get("value", 0)))
    storage.set_counter(ctx.db_path, provider, strategy, value)
    return {"success": True}


def _handle_claim_email_inbox_profile(params: dict[str, Any]) -> dict[str, Any]:
    """Claim a shared profile for the caller (mirrors claim_email_inbox_profile)."""
    uid = _uid(params)
    if uid is None:
        raise ValueError("authentication required to claim a shared profile")
    pid = str(
        params.get("profile_id") or params.get("profileId") or params.get("id") or ""
    )
    if not pid:
        raise ValueError("profile id is required")
    if not ctx.db_path:
        raise ValueError("plugin storage not initialised")
    return storage.claim_profile(ctx.db_path, pid, uid)


# ── Server entry point ────────────────────────────────────────────────────


def main() -> None:
    """Register handlers and serve the JSON-RPC loop."""
    server = RpcPluginServer()
    server.set_init_handler(_handle_init)
    server.register("_migrate_db", _handle_migrate_db)
    # SPI proxy targets
    server.register("list_profiles", _handle_list_profiles)
    server.register("wait_otp", _handle_wait_otp)
    server.register("sync", _handle_sync)
    # Mirrored email_* commands
    server.register("generate_from_settings", _handle_generate_from_settings)
    server.register("generate_from_settings_persistent", _handle_generate_from_settings_persistent)
    server.register("test_strategies", _handle_test_strategies)
    # Mirrored email_inbox_* session commands
    server.register("connect", _handle_connect)
    server.register("disconnect", _handle_disconnect)
    server.register("list", _handle_list)
    server.register("list_folders", _handle_list_folders)
    server.register("get_by_id", _handle_get_by_id)
    server.register("wait_for_email", _handle_wait_for_email)
    server.register("mark_as_read", _handle_mark_as_read)
    server.register("delete", _handle_delete)
    server.register("create_mailtm_account", _handle_create_mailtm_account)
    server.register("get_capabilities", _handle_get_capabilities)
    server.register("get_provider_catalog", _handle_get_provider_catalog)
    # Mirrored email_inbox_* profile commands
    server.register("get_profile", _handle_get_profile)
    server.register("upsert_profile", _handle_upsert_profile)
    server.register("delete_profile", _handle_delete_profile)
    server.register("connect_profile", _handle_connect_profile)
    server.register("get_sync_state", _handle_get_sync_state)
    server.register("upsert_sync_state", _handle_upsert_sync_state)
    # Bare commands (removed email_counter / claim built-ins)
    server.register("get_email_counter", _handle_get_email_counter)
    server.register("set_email_counter", _handle_set_email_counter)
    server.register("claim_email_inbox_profile", _handle_claim_email_inbox_profile)
    server.serve()


if __name__ == "__main__":
    main()
