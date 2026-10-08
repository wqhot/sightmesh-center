"""Deferred regression tests; do NOT run until consolidated validation."""
from pathlib import Path
import json
import tempfile
import unittest

from sightmesh_center.association_v1 import (
    AssociationPolicy, GreedySolver, OrToolsSolver,
    propose_candidates)
from sightmesh_center.global_tracks import GlobalTrackRepository
from sightmesh_center.track_inbox import DurableInbox
from sightmesh_center.tracklets import load_committed_tracklets


def ev(node, seq, timestamp, x, *, session=10, label=1,
       kind="start", map_valid=True, frame="local_enu_v1", local_id=1):
    return {
        "event_id": f"{node}/{session}/{seq}",
        "node_id": node, "camera_id": "camera0",
        "session_id": session, "local_id": local_id, "seq": seq,
        "type": kind, "reason": "track_started", "priority": 2,
        "track_state": 1, "frame_id": seq, "event_time_ns": timestamp,
        "media_timestamp_ms": timestamp // 1000000,
        "box": {"x": 0, "y": 0, "width": 10, "height": 10},
        "class_id": label, "confidence": 0.9, "blobs": [],
        "spatial": {
            "world": {
                "valid": True, "coordinate_frame_id": frame,
                "timestamp_ns": timestamp, "x_m": x, "y_m": 2.0, "z_m": 3.0,
                "velocity_valid": True, "vx_mps": 0.0,
                "vy_mps": 0.0, "vz_mps": 0.0,
                "position_covariance_m2": [1,0,0,0,1,0,0,0,1],
                "quality": 0.85,
            },
            "localization_quality": {
                "valid": True,
                "map": {"valid": map_valid, "revision": "map-same"}
            },
            "bearing": {"valid": False},
        },
    }


def envelope(*events):
    return json.dumps({"version": 1, "events": list(events)}).encode()


def policy(**updates):
    sources = {
        node: {"verified": True, "domain": "shared_sim",
               "offset_ns": 0, "clock_uncertainty_ns": 1_000_000,
               "alignment_id": "origin-same",
               "coordinate_frame_id": "local_enu_v1",
               "map_revision": "map-same"}
        for node in ("uav", "ugv")
    }
    for node, patch in updates.items():
        sources[node].update(patch)
    return AssociationPolicy.from_dict({"sources": sources})


class TrackletAssociationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "inbox.sqlite"
        self.inbox = DurableInbox(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def test_two_nodes_produce_pair_without_fake_state_fusion(self):
        self.inbox.store_batch(envelope(ev("uav", 1, 1_000_000_000, 10)))
        self.inbox.store_batch(envelope(ev("ugv", 1, 1_020_000_000, 10.5)))
        repo = GlobalTrackRepository(self.database)
        first = repo.recompute(policy())
        self.assertEqual(first["selected_pair_count"], 1)
        self.assertEqual(len(first["global_tracks"]), 1)
        world = first["global_tracks"][0]
        self.assertEqual(world["source_count"], 2)
        self.assertEqual(world["status"], "associated_pair")
        self.assertEqual(world["fusion"], "NOT_FUSED")
        self.assertEqual(world["representative"]["source"],
                         "representative_observation_not_fused")
        self.assertEqual(repo.recompute(policy())["world_revision"], first["world_revision"])
        self.assertEqual(GlobalTrackRepository(self.database).latest()["world_revision"],
                         first["world_revision"])

    def test_missing_frame_map_time_or_uncertainty_blocks_cross_node(self):
        self.inbox.store_batch(envelope(ev("uav", 1, 1_000_000_000, 10)))
        self.inbox.store_batch(envelope(ev("ugv", 1, 1_001_000_000, 10.5)))
        repo = GlobalTrackRepository(self.database)
        for pol in [
            policy(ugv={"verified": False}),
            policy(ugv={"alignment_id": "different-origin"}),
            policy(ugv={"map_revision": "map-other"}),
            policy(ugv={"domain": "shared_utc"}),
            policy(ugv={"clock_uncertainty_ns": 900_000_000}),
        ]:
            state = repo.recompute(pol)
            self.assertEqual(state["selected_pair_count"], 0)
            self.assertEqual(len(state["global_tracks"]), 2)

    def test_incomplete_blob_event_is_not_used_as_tracklet(self):
        import hashlib
        raw = b"missing-content"
        sha = hashlib.sha256(raw).hexdigest()
        a = ev("uav", 1, 1_000_000_000, 10)
        a["blobs"] = [{"sha256": sha, "size": len(raw),
                        "mime": "application/octet-stream"}]
        self.inbox.store_batch(envelope(a))
        self.assertEqual(load_committed_tracklets(self.database), [])
        self.inbox.store_blob(sha, raw)
        self.assertEqual(len(load_committed_tracklets(self.database)), 1)

    def test_late_source_event_can_revise_global_identity(self):
        self.inbox.store_batch(envelope(ev("uav", 1, 1_000_000_000, 10)))
        self.inbox.store_batch(envelope(ev("ugv", 1, 1_000_000_000, 10.3)))
        repo = GlobalTrackRepository(self.database)
        paired = repo.recompute(policy())
        first_id = paired["global_tracks"][0]["global_id"]
        self.inbox.store_batch(envelope(
            ev("ugv", 2, 1_010_000_000, 200, kind="update")))
        new = repo.recompute(policy())
        self.assertEqual(new["selected_pair_count"], 0)
        self.assertEqual(len(new["global_tracks"]), 2)
        changes = repo.revisions_after(paired["last_identity_revision"])
        self.assertTrue(any(change["old_global_id"] == first_id
                            and change["new_global_id"] != first_id
                            for change in changes))
        self.assertTrue(all(t["fusion"] == "NOT_FUSED"
                            for t in new["global_tracks"]))

    def test_overlap_local_id_different_nodes_not_same_tracklet(self):
        self.inbox.store_batch(envelope(ev("uav", 1, 1_000_000_000, 10)))
        self.inbox.store_batch(envelope(ev("ugv", 1, 1_010_000_000, 10)))
        items = load_committed_tracklets(self.database)
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0].uid, items[1].uid)
        self.assertEqual(len(propose_candidates(items, policy())), 1)

    def test_deterministic_greedy_and_ortools_optional(self):
        self.inbox.store_batch(envelope(ev("uav", 1, 1_000_000_000, 10)))
        self.inbox.store_batch(envelope(ev("ugv", 1, 1_010_000_000, 10)))
        items = load_committed_tracklets(self.database)
        candidates = propose_candidates(items, policy())
        self.assertEqual(len(GreedySolver().select(items, candidates)), 1)
        try:
            import ortools  # noqa: F401
        except ImportError:
            with self.assertRaises(RuntimeError):
                OrToolsSolver().select(items, candidates)
        else:
            self.assertEqual(len(OrToolsSolver().select(items, candidates)), 1)


if __name__ == "__main__":
    unittest.main()
