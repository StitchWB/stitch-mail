"""Minimal in-memory IMAP4rev1 server for plugin tests.

Speaks just enough of the wire protocol for a REAL ``imaplib`` client
(the plugin's own transport) to LOGIN, SELECT/EXAMINE, (UID) SEARCH,
(UID) FETCH (RFC822), (UID) STORE, EXPUNGE, LIST and LOGOUT.  Listens
on an ephemeral 127.0.0.1 port over plain TCP — tests connect with
``useTls: False`` credentials.
"""

from __future__ import annotations

import email as email_mod
import re
import socketserver
import threading
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

_TOKEN = re.compile(r'"[^"]*"|\S+')


def _unquote(value: str) -> str:
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


class _Mailbox:
    """Shared mailbox state behind every fake connection."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.seen: set[int] = set()
        self.deleted: set[int] = set()
        self.logins: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def add(self, subject: str, body: str, *, to: str = "", age_s: float = 0.0) -> None:
        with self.lock:
            self.messages.append(
                {
                    "subject": subject,
                    "body": body,
                    "to": to,
                    "date": datetime.now(UTC) - timedelta(seconds=age_s),
                }
            )

    def message_bytes(self, seq: int) -> bytes:
        with self.lock:
            data = self.messages[seq - 1]
        msg = email_mod.message_from_string("")
        msg["Subject"] = data["subject"]
        msg["From"] = "sender@example.com"
        msg["To"] = data["to"]
        msg["Date"] = format_datetime(data["date"])
        msg.set_payload(data["body"])
        return msg.as_bytes()


class _ImapHandler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        self.connection = self.request
        self.rfile = self.connection.makefile("rb")
        self.wfile = self.connection.makefile("wb")

    def _send(self, data: bytes) -> None:
        self.wfile.write(data)
        self.wfile.flush()

    def _line(self, text: str) -> None:
        self._send(text.encode() + b"\r\n")

    def handle(self) -> None:
        box: _Mailbox = self.server.mailbox  # type: ignore[attr-defined]
        self._line("* OK fake IMAP4rev1 ready")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            request = raw.decode("utf-8", errors="replace").strip()
            if not request:
                continue
            parts = request.split(" ", 2)
            if len(parts) < 2:
                continue
            tag, command = parts[0], parts[1].upper()
            args = parts[2] if len(parts) > 2 else ""
            if command == "LOGOUT":
                self._line("* BYE fake IMAP signing off")
                self._line(f"{tag} OK LOGOUT completed")
                return
            self._dispatch(box, tag, command, args)

    def _dispatch(self, box: _Mailbox, tag: str, command: str, args: str) -> None:
        if command == "CAPABILITY":
            self._line("* CAPABILITY IMAP4rev1")
            self._line(f"{tag} OK CAPABILITY completed")
        elif command == "NOOP":
            self._line(f"{tag} OK NOOP completed")
        elif command == "LOGIN":
            tokens = _TOKEN.findall(args)
            user = _unquote(tokens[0]) if tokens else ""
            password = _unquote(tokens[1]) if len(tokens) > 1 else ""
            box.logins.append((user, password))
            self._line(f"{tag} OK LOGIN completed")
        elif command in ("SELECT", "EXAMINE"):
            with box.lock:
                count = len(box.messages)
            self._line(f"* {count} EXISTS")
            self._line("* 0 RECENT")
            self._line("* FLAGS (\\Seen \\Deleted)")
            mode = "READ-WRITE" if command == "SELECT" else "READ-ONLY"
            self._line(f"{tag} OK [{mode}] {command} completed")
        elif command == "SEARCH":
            self._search(box, tag, args)
        elif command == "FETCH":
            self._fetch(box, tag, args)
        elif command == "STORE":
            self._store(box, tag, args)
        elif command == "EXPUNGE":
            self._expunge(box, tag)
        elif command == "LIST":
            self._line('* LIST (\\HasNoChildren) "/" "INBOX"')
            self._line('* LIST (\\HasNoChildren) "/" "Sent"')
            self._line(f"{tag} OK LIST completed")
        elif command == "UID":
            sub = args.split(" ", 1)
            sub_command = sub[0].upper()
            sub_args = sub[1] if len(sub) > 1 else ""
            if sub_command == "SEARCH":
                self._search(box, tag, sub_args)
            elif sub_command == "FETCH":
                self._fetch(box, tag, sub_args)
            elif sub_command == "STORE":
                self._store(box, tag, sub_args)
            else:
                self._line(f"{tag} BAD unknown UID subcommand {sub_command}")
        else:
            self._line(f"{tag} BAD unknown command {command}")

    def _search(self, box: _Mailbox, tag: str, criteria: str) -> None:
        with box.lock:
            ids = [
                str(i + 1)
                for i in range(len(box.messages))
                if "UNSEEN" not in criteria.upper() or (i + 1) not in box.seen
            ]
        joined = " ".join(ids)
        self._line(f"* SEARCH {joined}" if joined else "* SEARCH")
        self._line(f"{tag} OK SEARCH completed")

    def _fetch(self, box: _Mailbox, tag: str, args: str) -> None:
        token = args.split(" ", 1)[0]
        try:
            seq = int(token)
        except ValueError:
            self._line(f"{tag} BAD bad sequence {token}")
            return
        with box.lock:
            exists = 1 <= seq <= len(box.messages)
        if not exists:
            self._line(f"{tag} OK FETCH completed")
            return
        raw = box.message_bytes(seq)
        self._send(f"* {seq} FETCH (UID {seq} RFC822 {{{len(raw)}}}\r\n".encode())
        self._send(raw)
        self._send(b")\r\n")
        self._line(f"{tag} OK FETCH completed")

    def _store(self, box: _Mailbox, tag: str, args: str) -> None:
        tokens = args.split(" ", 2)
        try:
            seq = int(tokens[0])
        except (IndexError, ValueError):
            self._line(f"{tag} BAD bad STORE {args}")
            return
        flags = tokens[2] if len(tokens) > 2 else ""
        with box.lock:
            if "\\Seen" in flags:
                box.seen.add(seq)
            if "\\Deleted" in flags:
                box.deleted.add(seq)
            exists = 1 <= seq <= len(box.messages)
        if exists:
            self._line(f"* {seq} FETCH (FLAGS (\\Seen))")
        self._line(f"{tag} OK STORE completed")

    def _expunge(self, box: _Mailbox, tag: str) -> None:
        with box.lock:
            for seq in sorted(box.deleted, reverse=True):
                if 1 <= seq <= len(box.messages):
                    del box.messages[seq - 1]
            box.deleted.clear()
            box.seen.clear()
        self._line(f"{tag} OK EXPUNGE completed")


class FakeImapServer:
    """Start/stop handle around the threaded fake IMAP server."""

    def __init__(self) -> None:
        self.mailbox = _Mailbox()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    def add_message(
        self, subject: str, body: str, *, to: str = "", age_s: float = 0.0
    ) -> None:
        self.mailbox.add(subject, body, to=to, age_s=age_s)

    @property
    def logins(self) -> list[tuple[str, str]]:
        return self.mailbox.logins

    @property
    def seen(self) -> set[int]:
        return self.mailbox.seen

    def start(self) -> tuple[str, int]:
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _ImapHandler)
        server.mailbox = self.mailbox  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return "127.0.0.1", server.server_address[1]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
