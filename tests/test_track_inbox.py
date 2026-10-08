"""Deferred tests: durable ACK, duplicates, ordering, blobs, restart, HTTP auth."""
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from sightmesh_center.track_inbox import DurableInbox, InboxError
from sightmesh_center.track_ingest_server import serve_inbox


def event(seq: int, *, node: str = "tracker_uav_1",
          session: int = 42, blobs=None, event_id=None) -> dict:
    # Minimal projection of JsonProtocolCodec::EncodeEventObject; Center keeps
    # the untouched complete JSON event, including "spatial" if supplied.
    return {
        "event_id": event_id or f"evt-{session}-{seq}",
        "seq": seq,
        "node_id": node,
        "camera_id": "camera0",
        "session_id": session,
        "local_id": 7,
        "type": "start" if seq == 1 else "update",
        "reason": "track_updated",
        "priority": 2,
        "track_state": 1,
        "frame_id": seq,
        "event_time_ns": 1700000000000000000 + seq,
        "media_timestamp_ms": seq * 40,
        "box": {"x": 1, "y": 2, "width": 3, "height": 4},
        "class_id": 0,
        "confidence": 0.95,
        "blobs": blobs or [],
        "spatial": {"world": {"valid": True, "coordinate_frame_id": "local_enu_v1",
                              "x_m": 1.25, "y_m": 5.0, "z_m": 10.0}},
    }


def batch(*events) -> bytes:
    return json.dumps({"version": 1, "events": list(events)}).encode()


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "center-inbox.sqlite3"
        self.inbox = DurableInbox(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_durable_contiguous_ack_out_of_order_and_reboot(self):
        ack = self.inbox.store_batch(batch(event(2), event(4)))
        self.assertEqual(ack["highest_contiguous_seq"], 0)
        self.assertEqual(ack["missing"], [[1, 1], [3, 3]])
        ack = self.inbox.store_batch(batch(event(1)))
        self.assertEqual(ack["highest_contiguous_seq"], 2)
        self.assertEqual(ack["missing"], [[3, 3]])
        self.assertEqual(self.inbox.store_batch(batch(event(3)))["highest_contiguous_seq"], 4)
        # Journal seq is persistent across source restart; session ID changes.
        ack = self.inbox.store_batch(batch(event(5, session=99)))
        self.assertEqual(ack["highest_contiguous_seq"], 5)
        self.inbox = DurableInbox(self.path)
        self.assertEqual(self.inbox.source_status("tracker_uav_1", 99)["highest_contiguous_seq"], 5)
        self.assertEqual(len(self.inbox.read_events("tracker_uav_1", 42)), 5)
        self.assertEqual(self.inbox.read_events("tracker_uav_1", 42)[4]["session_id"], 99)

    def test_same_event_idempotent_conflict_never_advances(self):
        original = event(1)
        self.inbox.store_batch(batch(original))
        self.assertEqual(self.inbox.store_batch(batch(original))["highest_contiguous_seq"], 1)
        altered = dict(original, class_id=9)
        with self.assertRaisesRegex(InboxError, "conflicts"):
            self.inbox.store_batch(batch(altered))
        with self.assertRaises(InboxError):
            self.inbox.store_batch(batch(event(2, event_id=original["event_id"])))
        self.assertEqual(self.inbox.source_status("tracker_uav_1", 42)["highest_contiguous_seq"], 1)
        self.assertEqual(len(self.inbox.read_events("tracker_uav_1", 42)), 1)

    def test_blob_reference_blocks_ack_until_content_hash_matches(self):
        data = b"\0not JPEG synthetic blob"
        sha = hashlib.sha256(data).hexdigest()
        ref = {"sha256": sha, "size": len(data), "mime": "application/octet-stream"}
        a = self.inbox.store_batch(batch(event(1), event(2, blobs=[ref])))
        self.assertTrue(a["accepted"])
        self.assertEqual(a["highest_contiguous_seq"], 1)
        self.assertEqual(a["missing_blobs"], [sha])
        with self.assertRaisesRegex(InboxError, "SHA256 mismatch"):
            self.inbox.store_blob(sha, b"corrupt", "application/octet-stream")
        self.assertEqual(self.inbox.source_status("tracker_uav_1", 42)["highest_contiguous_seq"], 1)
        self.inbox.store_blob(sha, data)
        self.assertEqual(self.inbox.source_status("tracker_uav_1", 42)["highest_contiguous_seq"], 2)
        self.inbox = DurableInbox(self.path)
        self.assertEqual(self.inbox.source_status("tracker_uav_1", 42)["highest_contiguous_seq"], 2)
        self.inbox.store_blob(sha, data)  # safe replay

    def test_atomic_transaction_rejects_mixed_source_or_bad_event(self):
        good = event(1)
        bad = event(2, node="other")
        with self.assertRaisesRegex(InboxError, "mixed node"):
            self.inbox.store_batch(batch(good, bad))
        self.assertEqual(self.inbox.read_events("tracker_uav_1", 42), [])
        bad2 = dict(event(2), seq=-5)
        with self.assertRaises(InboxError):
            self.inbox.store_batch(batch(good, bad2))
        self.assertEqual(self.inbox.read_events("tracker_uav_1", 42), [])

    def test_source_spool_reset_is_detected(self):
        self.inbox.store_batch(batch(event(1)))
        with self.assertRaisesRegex(InboxError, "conflicts"):
            self.inbox.store_batch(batch(event(1, session=99)))
        self.assertEqual(self.inbox.source_status("tracker_uav_1", 99)["highest_contiguous_seq"], 1)


class InboxHttpTests(unittest.TestCase):
    def test_auth_and_http_edge_url_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            token.write_text("change-this-long-development-token-123456")
            token.chmod(0o600)
            server = serve_inbox("127.0.0.1", 0, Path(tmp) / "db.sqlite3", token)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def request(method, path, data, headers=None):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
                    conn.request(method, path, body=data, headers=headers or {})
                    response = conn.getresponse()
                    status, raw = response.status, response.read()
                    conn.close()
                    return status, json.loads(raw)
                path = "/api/v1/mtmct/events/batch"
                body = batch(event(1))
                code, _ = request("POST", path, body, {"Content-Type": "application/json"})
                self.assertEqual(code, 401)
                auth = {"Authorization": "Bearer " + token.read_text(),
                        "Content-Type": "application/json"}
                code, ack = request("POST", path, body, auth)
                self.assertEqual(code, 200)
                self.assertEqual(ack["highest_contiguous_seq"], 1)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
