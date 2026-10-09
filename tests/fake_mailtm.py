"""Minimal mail.tm REST stub for plugin tests.

Serves the endpoints the plugin's mail.tm client uses: ``GET /domains``,
``POST /accounts``, ``POST /token``, ``GET /messages``, ``GET/PATCH/DELETE
/messages/{id}`` — all on an ephemeral 127.0.0.1 port, hydra-shaped JSON.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeMailTmServer:
    def __init__(self) -> None:
        self.accounts: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.tokens: dict[str, str] = {}
        self.created: list[dict[str, str]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def add_message(self, subject: str, text: str, *, intro: str = "") -> None:
        self.messages.append(
            {
                "id": str(len(self.messages) + 1),
                "msgid": f"<{len(self.messages) + 1}@fake.tm>",
                "from": {"name": "Sender", "address": "sender@fake.tm"},
                "to": [{"name": "", "address": "box@fake.tm"}],
                "subject": subject,
                "intro": intro or text[:40],
                "text": text,
                "html": [f"<p>{text}</p>"],
                "seen": False,
                "createdAt": "2026-01-01T00:00:00+00:00",
            }
        )

    def start(self) -> str:
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                return json.loads(self.rfile.read(length))

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                if path == "/domains":
                    self._json(
                        200,
                        {"hydra:member": [{"id": "d1", "domain": "fake.tm", "isActive": True}]},
                    )
                elif path == "/messages":
                    self._json(200, {"hydra:member": outer.messages})
                elif path.startswith("/messages/"):
                    mid = path.rsplit("/", 1)[1]
                    msg = next((m for m in outer.messages if m["id"] == mid), None)
                    if msg is None:
                        self._json(404, {"message": "not found"})
                    else:
                        self._json(200, msg)
                else:
                    self._json(404, {"message": "not found"})

            def do_POST(self) -> None:
                body = self._body()
                if self.path.split("?")[0] == "/accounts":
                    outer.created.append(
                        {"address": str(body.get("address", "")), "password": str(body.get("password", ""))}
                    )
                    account = {"id": f"acc-{len(outer.accounts) + 1}", "address": body.get("address", "")}
                    outer.accounts.append(account)
                    self._json(201, account)
                elif self.path.split("?")[0] == "/token":
                    token = f"tok-{len(outer.tokens) + 1}"
                    outer.tokens[token] = str(body.get("address", ""))
                    self._json(200, {"token": token, "id": "acc-1"})
                else:
                    self._json(404, {"message": "not found"})

            def do_PATCH(self) -> None:
                path = self.path.split("?")[0]
                if path.startswith("/messages/"):
                    mid = path.rsplit("/", 1)[1]
                    body = self._body()
                    for msg in outer.messages:
                        if msg["id"] == mid:
                            msg["seen"] = bool(body.get("seen", False))
                    self._json(200, {"id": mid, "seen": body.get("seen", False)})
                else:
                    self._json(404, {"message": "not found"})

            def do_DELETE(self) -> None:
                path = self.path.split("?")[0]
                if path.startswith("/messages/"):
                    mid = path.rsplit("/", 1)[1]
                    outer.messages = [m for m in outer.messages if m["id"] != mid]
                    self.send_response(204)
                    self.end_headers()
                elif path.startswith("/accounts/"):
                    self.send_response(204)
                    self.end_headers()
                else:
                    self._json(404, {"message": "not found"})

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
