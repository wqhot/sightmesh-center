"""Durable, idempotent inbox for Edge's EXISTING JSON TrackEvent/Blob protocol.

Storage is deliberately transport-neutral: both the original HTTP Push and
future Wire v1 NNG REQ/REP adapters must call this same commit logic. ACK
means a contiguous prefix of complete (event + referenced blobs) committed to
SQLite with synchronous=FULL, never merely bytes received into process RAM.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

MAX_BATCH_BYTES = 4 * 1024 * 1024
MAX_BLOB_BYTES = 16 * 1024 * 1024
MAX_EVENTS_PER_BATCH = 512
MAX_SEQ = (1 << 63) - 1
MAX_U64 = (1 << 64) - 1
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ALLOWED_EVENTS = {"start", "update", "end"}


class InboxError(ValueError):
    """Bad or conflicting input. Its transaction MUST be rolled back."""


def _object_unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, val in pairs:
        if key in result:
            raise InboxError(f"duplicate JSON field: {key}")
        result[key] = val
    return result


def _nonfinite(value: str) -> None:
    raise InboxError(f"non-finite JSON value: {value}")


def _uint(value: Any, field: str, *, allow_zero: bool = True, limit: int = MAX_SEQ) -> int:
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= limit:
        raise InboxError(f"{field} must be a nonnegative signed-64-bit integer")
    return value


def _text(value: Any, field: str, max_length: int = 256) -> str:
    if (not isinstance(value, str) or not value or len(value) > max_length
            or any(ord(char) < 32 for char in value)):
        raise InboxError(f"{field} must be a nonempty identifier")
    return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise InboxError("invalid lowercase SHA256 content ID")
    return value


def _decode(raw: bytes) -> list[dict]:
    if not raw or len(raw) > MAX_BATCH_BYTES:
        raise InboxError("event batch length is invalid")
    try:
        message = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_unique,
                             parse_constant=_nonfinite)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InboxError(f"invalid JSON batch: {exc}") from exc
    if type(message) is not dict or type(message.get("version")) is not int or message["version"] != 1:
        raise InboxError("unsupported event batch version")
    events = message.get("events")
    if type(events) is not list or not 1 <= len(events) <= MAX_EVENTS_PER_BATCH:
        raise InboxError("events must be nonempty and within batch limit")
    return events


def _validate_event(event: Any) -> tuple[str, str, int, str, str, list[tuple[str, int, str]]]:
    if type(event) is not dict:
        raise InboxError("event must be an object")
    node = _text(event.get("node_id"), "node_id", 128)
    _text(event.get("camera_id"), "camera_id", 128)
    session = str(_uint(event.get("session_id"), "session_id", limit=MAX_U64))
    seq = _uint(event.get("seq"), "seq", allow_zero=False)
    event_id = _text(event.get("event_id"), "event_id", 512)
    if event.get("type") not in ALLOWED_EVENTS:
        raise InboxError("unknown TrackEvent lifecycle type")
    _uint(event.get("local_id"), "local_id")
    _uint(event.get("event_time_ns"), "event_time_ns")
    blobs = event.get("blobs")
    if type(blobs) is not list or len(blobs) > 128:
        raise InboxError("event.blobs must be a bounded list")
    refs = []
    for item in blobs:
        if type(item) is not dict:
            raise InboxError("blob reference must be an object")
        sha = _sha(item.get("sha256"))
        size = _uint(item.get("size"), "blob.size")
        if size > MAX_BLOB_BYTES:
            raise InboxError("blob exceeds size limit")
        mime = item.get("mime")
        if not isinstance(mime, str) or len(mime) > 200:
            raise InboxError("invalid blob MIME")
        refs.append((sha, size, mime))
    if len(set(sha for sha, _, _ in refs)) != len(refs):
        raise InboxError("duplicate blob reference in event")
    payload = json.dumps(event, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return node, session, seq, event_id, digest, refs


class DurableInbox:
    def __init__(self, database: str | Path):
        self.path = Path(database).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources(
                  node TEXT PRIMARY KEY,
                  contiguous INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events(
                  node TEXT NOT NULL, session TEXT NOT NULL,
                  seq INTEGER NOT NULL, event_id TEXT NOT NULL,
                  event_time_ns INTEGER NOT NULL,
                  camera TEXT NOT NULL, local_id INTEGER NOT NULL,
                  payload TEXT NOT NULL, digest TEXT NOT NULL,
                  PRIMARY KEY(node, seq),
                  UNIQUE(node, event_id),
                  FOREIGN KEY(node) REFERENCES sources(node)
                );
                CREATE TABLE IF NOT EXISTS blobs(
                  sha TEXT PRIMARY KEY, size INTEGER NOT NULL,
                  mime TEXT NOT NULL, contents BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_blobs(
                  node TEXT NOT NULL,
                  seq INTEGER NOT NULL, sha TEXT NOT NULL,
                  size INTEGER NOT NULL, mime TEXT NOT NULL,
                  PRIMARY KEY(node, seq, sha),
                  FOREIGN KEY(node, seq)
                    REFERENCES events(node, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_event_blobs_sha
                    ON event_blobs(sha);
            """)

    # The Edge spool has ONE persistent sequence per node across restarts.
    # session_id changes each process startup, so ACK cannot be session-scoped.
    # A fresh spool reusing the same node ID must be explicitly reprovisioned.

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _advance(db: sqlite3.Connection, node: str) -> int:
        current = db.execute(
            "SELECT contiguous FROM sources WHERE node=?",
            (node,)).fetchone()[0]
        # A missing event OR a missing referenced blob prevents ACKing past
        # that sequence. This watermark is persisted IN THE SAME transaction.
        while True:
            next_seq = current + 1
            found = db.execute(
                "SELECT 1 FROM events WHERE node=? AND seq=?",
                (node, next_seq)).fetchone()
            if not found:
                break
            missing = db.execute("""
                SELECT 1 FROM event_blobs AS eb
                LEFT JOIN blobs AS b ON b.sha=eb.sha AND b.size=eb.size
                WHERE eb.node=? AND eb.seq=?
                      AND b.sha IS NULL LIMIT 1
            """, (node, next_seq)).fetchone()
            if missing:
                break
            current = next_seq
        db.execute(
            "UPDATE sources SET contiguous=? WHERE node=?",
            (current, node))
        return current

    @staticmethod
    def _ack(db: sqlite3.Connection, node: str) -> dict:
        cursor = db.execute(
            "SELECT contiguous FROM sources WHERE node=?",
            (node,)).fetchone()
        contiguous = cursor[0] if cursor else 0
        max_seq = db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM events WHERE node=?",
            (node,)).fetchone()[0]
        missing = []
        expected = contiguous + 1
        for (seq,) in db.execute(
            "SELECT seq FROM events WHERE node=? AND seq>? ORDER BY seq",
            (node, contiguous)):
            if seq > expected:
                missing.append([expected, seq - 1])
            expected = seq + 1
            if len(missing) >= 128:
                break
        if len(missing) < 128 and expected <= max_seq:
            missing.append([expected, max_seq])
        missing_blobs = [row[0] for row in db.execute("""
            SELECT DISTINCT eb.sha FROM event_blobs AS eb
            JOIN events AS e ON e.node=eb.node AND e.seq=eb.seq
            LEFT JOIN blobs AS b ON b.sha=eb.sha AND b.size=eb.size
            WHERE e.node=? AND e.seq>? AND b.sha IS NULL
            ORDER BY eb.sha LIMIT 128
        """, (node, contiguous))]
        return {
            "accepted": True,
            "highest_contiguous_seq": contiguous,
            "missing": missing,
            "missing_blobs": missing_blobs,
            "message": "committed",
        }

    def store_batch(self, raw: bytes) -> dict:
        events = _decode(raw)
        validated = [_validate_event(event) for event in events]
        node, session = validated[0][:2]
        if any(pair[0] != node for pair in validated):
            raise InboxError("mixed node batch is not allowed")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT OR IGNORE INTO sources(node) VALUES(?)",
                    (node,))
                for event, (e_node, e_session, seq, event_id, digest, refs) in zip(events, validated):
                    prev = db.execute(
                        "SELECT event_id,digest FROM events WHERE node=? AND seq=?",
                        (e_node, seq)).fetchone()
                    if prev:
                        if prev != (event_id, digest):
                            raise InboxError("sequence replay conflicts with durable event")
                        continue
                    payload = json.dumps(event, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":"), allow_nan=False)
                    db.execute("""
                        INSERT INTO events(node,session,seq,event_id,event_time_ns,
                                           camera,local_id,payload,digest)
                        VALUES(?,?,?,?,?,?,?,?,?)
                    """, (e_node, e_session, seq, event_id,
                          event["event_time_ns"], event["camera_id"],
                          event["local_id"], payload, digest))
                    for sha, size, mime in refs:
                        db.execute("""
                            INSERT INTO event_blobs(node,seq,sha,size,mime)
                            VALUES(?,?,?,?,?)
                        """, (e_node, seq, sha, size, mime))
                self._advance(db, node)
                response = self._ack(db, node)
                db.execute("COMMIT")
                return response
            except (sqlite3.IntegrityError, InboxError) as exc:
                db.execute("ROLLBACK")
                raise InboxError(str(exc)) from exc
            except Exception:
                db.execute("ROLLBACK")
                raise

    def store_blob(self, sha: str, data: bytes, mime: str = "application/octet-stream") -> dict:
        sha = _sha(sha)
        if len(data) > MAX_BLOB_BYTES:
            raise InboxError("blob exceeds maximum length")
        if not isinstance(mime, str) or len(mime) > 200:
            raise InboxError("invalid MIME type")
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), sha):
            raise InboxError("blob SHA256 mismatch")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT size,contents FROM blobs WHERE sha=?", (sha,)).fetchone()
                if existing is not None:
                    if existing[0] != len(data) or existing[1] != data:
                        raise InboxError("conflicting content addressed blob")
                else:
                    db.execute(
                        "INSERT INTO blobs(sha,size,mime,contents) VALUES(?,?,?,?)",
                        (sha, len(data), mime, sqlite3.Binary(data)))
                # A blob may unblock pending events from MULTIPLE sources.
                affected = db.execute(
                    "SELECT DISTINCT node FROM event_blobs WHERE sha=?",
                    (sha,)).fetchall()
                for (node,) in affected:
                    self._advance(db, node)
                db.execute("COMMIT")
            except (sqlite3.IntegrityError, InboxError) as exc:
                db.execute("ROLLBACK")
                raise InboxError(str(exc)) from exc
            except Exception:
                db.execute("ROLLBACK")
                raise
        return {"sha256": sha, "size": len(data), "stored": True}

    def source_status(self, node: str, session_id: int) -> dict:
        node = _text(node, "node_id", 128)
        _uint(session_id, "session_id", limit=MAX_U64)  # compatibility; ACK scope is node journal
        with closing(self._connect()) as db:
            return self._ack(db, node)

    def read_events(self, node: str, session_id: int, after: int = 0, limit: int = 100) -> list[dict]:
        node = _text(node, "node_id", 128)
        _uint(session_id, "session_id", limit=MAX_U64)  # compatibility; ACK scope is node journal
        _uint(after, "after")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InboxError("invalid page limit")
        with closing(self._connect()) as db:
            rows = db.execute("""
                SELECT payload FROM events WHERE node=? AND seq>?
                ORDER BY seq LIMIT ?
            """, (node, after, limit)).fetchall()
            return [json.loads(row[0]) for row in rows]
