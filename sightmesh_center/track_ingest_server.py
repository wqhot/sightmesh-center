"""Optional standalone Center TrackEvent HTTP ingest service.

Runs on its OWN port, leaving the existing read-only Python/C++ map service
unchanged. Handler delegates all persistence and ACK decisions to DurableInbox.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import stat
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .track_inbox import (
    DurableInbox, InboxError, MAX_BATCH_BYTES, MAX_BLOB_BYTES,
)


class InboxHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, inbox: DurableInbox, token: str | None):
        self.inbox = inbox
        self.token = token
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: InboxHttpServer

    def _reply(self, code: int, payload: dict) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def _authorized(self) -> bool:
        # Avoid accidentally accepting an unprotected LAN writer. Remote
        # deployments must set the same token in Edge's transport config and
        # protect plaintext HTTP with WireGuard/TLS VPN / isolated network.
        if self.server.token is None:
            return True
        header = self.headers.get("Authorization", "")
        if not hmac.compare_digest(header, "Bearer " + self.server.token):
            self._reply(401, {"error": "unauthorized"})
            return False
        return True

    def _body(self, maximum: int) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise InboxError("chunked/streaming upload is not supported")
        value = self.headers.get("Content-Length")
        if value is None or not value.isascii() or not value.isdecimal():
            raise InboxError("Content-Length is required")
        size = int(value)
        if size < 1 or size > maximum:
            raise InboxError("request length exceeds limit or is empty")
        body = self.rfile.read(size)
        if len(body) != size:
            raise InboxError("truncated request body")
        return body

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/health":
            self._reply(200, {"service": "sightmesh-track-inbox", "status": "running"})
            return
        if path not in ("/api/v1/mtmct/global-tracks",
                        "/api/v1/mtmct/global-id-revisions"):
            self._reply(404, {"error": "not found"})
            return
        if not self._authorized():
            return
        from .global_tracks import GlobalTrackRepository
        try:
            repository = GlobalTrackRepository(self.server.inbox.path)
            if path.endswith("/global-tracks"):
                self._reply(200, repository.latest())
            else:
                params = parse_qs(parsed.query, strict_parsing=True)
                if set(params) - {"after", "limit"}:
                    raise ValueError("unknown revision query parameter")
                after = int(params.get("after", ["0"])[0])
                limit = int(params.get("limit", ["100"])[0])
                self._reply(200, {
                    "schema_major": 1,
                    "identity_revisions": repository.revisions_after(after, limit)
                })
        except (ValueError, OverflowError) as exc:
            self._reply(400, {"error": str(exc)})
        except Exception:
            self.log_exception()
            self._reply(503, {"error": "global state query unavailable"})

    def do_POST(self):
        if urlsplit(self.path).path != "/api/v1/mtmct/events/batch":
            self._reply(404, {"error": "not found"})
            return
        if not self._authorized():
            return
        if self.headers.get_content_type() != "application/json":
            self._reply(415, {"error": "Content-Type must be application/json"})
            return
        try:
            response = self.server.inbox.store_batch(self._body(MAX_BATCH_BYTES))
            self._reply(200, response)
        except InboxError as exc:
            self._reply(409, {
                "accepted": False,
                "highest_contiguous_seq": 0,
                "missing": [],
                "missing_blobs": [],
                "message": str(exc),
            })
        except Exception:
            self.log_exception()
            self._reply(503, {"error": "inbox transaction failed"})

    def do_PUT(self):
        path = urlsplit(self.path).path
        prefix = "/api/v1/mtmct/blobs/"
        if not path.startswith(prefix) or "/" in path[len(prefix):]:
            self._reply(404, {"error": "not found"})
            return
        if not self._authorized():
            return
        sha = path[len(prefix):]
        try:
            data = self._body(MAX_BLOB_BYTES)
            claimed_size = self.headers.get("X-MTMCT-Blob-Size")
            claimed_sha = self.headers.get("X-MTMCT-Blob-SHA256")
            if claimed_sha != sha or claimed_size != str(len(data)):
                raise InboxError("Blob metadata headers do not match body")
            response = self.server.inbox.store_blob(
                sha, data, mime=self.headers.get_content_type())
            self._reply(200, response)
        except InboxError as exc:
            self._reply(409, {"error": str(exc)})
        except Exception:
            self.log_exception()
            self._reply(503, {"error": "blob transaction failed"})

    def log_exception(self):
        import logging
        logging.exception("track inbox HTTP failure")

    def log_message(self, format, *args):
        # Avoid logging bearer tokens or raw event JSON.
        import sys
        sys.stderr.write("[sightmesh-inbox] %s - %s\n" %
                         (self.address_string(), format % args))


def serve_inbox(bind: str = "127.0.0.1", port: int = 18081,
                db_path: str | Path = "data/track-inbox.sqlite3",
                token_file: str | Path | None = None) -> InboxHttpServer:
    if not 0 <= port <= 65535:
        raise ValueError("invalid TCP port")
    token = None
    if token_file is not None:
        mode = Path(token_file).stat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError("token file must be a regular owner-only file (chmod 600)")
        token = Path(token_file).read_text(encoding="utf-8").strip()
        if not (24 <= len(token) <= 256) or any(c.isspace() for c in token):
            raise ValueError("token file must contain >=24 characters, with no whitespace")
    if bind not in ("127.0.0.1", "::1", "localhost") and not token:
        raise ValueError("refusing to expose writable inbox on LAN without --token-file")
    return InboxHttpServer((bind, port), DurableInbox(db_path), token)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Center durable Edge TrackEvent inbox")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--db", type=Path, default=Path("data/track-inbox.sqlite3"))
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args(argv)
    try:
        with serve_inbox(args.bind, args.port, args.db, args.token_file) as server:
            print(f"Track inbox on {args.bind}:{server.server_port} ({args.db})", flush=True)
            server.serve_forever()
    except (OSError, ValueError) as exc:
        parser.exit(2, f"track inbox startup error: {exc}\n")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
