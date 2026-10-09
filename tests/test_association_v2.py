"""Staged V2 bearing/clique regressions, run only during final joint validation."""
from pathlib import Path
import json
import tempfile
import unittest

from sightmesh_center.association_v1 import AssociationPolicy, propose_candidates
from sightmesh_center.association_v2 import form_consistent_groups
from sightmesh_center.bearing_geometry import (
    read_ray, assess_rays, bearing_pair_consistent)
from sightmesh_center.global_tracks import GlobalTrackRepository
from sightmesh_center.track_inbox import DurableInbox
from sightmesh_center.tracklets import load_committed_tracklets


def make_event(node, x=10, *, direction=None, observer=None):
    result = {
        "event_id": f"{node}-1", "node_id": node,
        "camera_id": "camera0", "session_id": 9, "local_id": 1,
        "seq": 1, "type": "start", "reason": "track_started",
        "frame_id": 1, "event_time_ns": 1_000_000_000,
        "class_id": 0, "confidence": 0.9, "blobs": [],
        "spatial": {
            "world": {
                "valid": True, "coordinate_frame_id": "local_enu_v1",
                "x_m": x, "y_m": 2, "z_m": 3, "quality": 0.9,
                "position_covariance_m2": [1,0,0,0,1,0,0,0,1],
            },
            "localization_quality": {
                "map": {"valid": True, "revision": "map-1"}
            },
            "bearing": {"valid": False},
        },
    }
    if observer is not None:
        result["spatial"]["bearing"] = {
            "valid": True,
            "camera_position_world_m": list(observer),
            "direction_world": list(direction),
            "has_tangent_covariance": True,
            "tangent_covariance_rad2": [0.0001, 0, 0, 0.0001],
            "timestamp_ns": 1_000_000_000,
        }
    return result


def config():
    nodes = ("a", "b", "c")
    return AssociationPolicy.from_dict({
        "sources": {node: {
            "verified": True, "domain": "shared_sim",
            "offset_ns": 0, "clock_uncertainty_ns": 100_000,
            "alignment_id": "same_enu_origin",
            "coordinate_frame_id": "local_enu_v1",
            "map_revision": "map-1",
        } for node in nodes},
        "class_labels": {node: {"0": "tank"} for node in nodes},
        "minimum_ray_crossing_deg": 3,
        "maximum_ray_separation_sigma": 4,
    })


class MultiNodeBearingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Path(self.temporary.name) / "inbox.sqlite"
        self.inbox = DurableInbox(self.db)

    def tearDown(self):
        self.temporary.cleanup()

    def insert(self, *events):
        for event in events:
            self.inbox.store_batch(json.dumps({
                "version": 1, "events": [event]
            }).encode("utf-8"))

    def test_geometry_forward_and_conflicting_ray(self):
        a = read_ray(make_event("a", direction=(1,0,0),
                                 observer=(0,2,3))["spatial"]["bearing"])
        b = read_ray(make_event("b", direction=(0,1,0),
                                 observer=(10,-8,3))["spatial"]["bearing"])
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        evidence = assess_rays(a,b,1,0)
        self.assertTrue(evidence.forward)
        self.assertTrue(evidence.informative)
        self.assertAlmostEqual(evidence.separation_m, 0)
        self.assertTrue(bearing_pair_consistent(a,b,1,0,3,4)[0])
        wrong = read_ray(make_event("b", direction=(0,-1,0),
                                     observer=(10,-8,3))["spatial"]["bearing"])
        self.assertFalse(bearing_pair_consistent(a,wrong,1,0,3,4)[0])

    def test_near_parallel_rays_are_uninformative_not_depth(self):
        a = read_ray(make_event("a", direction=(1,0,0),
                                 observer=(0,2,3))["spatial"]["bearing"])
        b = read_ray(make_event("b", direction=(1,0,0),
                                 observer=(0,-8,3))["spatial"]["bearing"])
        ok, reason, info = bearing_pair_consistent(a,b,1,0,3,4)
        self.assertTrue(ok)
        self.assertFalse(info.informative)
        self.assertEqual(reason, "weak-parallax-not-used")

    def test_three_node_full_clique_and_identity_provenance(self):
        self.insert(
            make_event("a", direction=(1,0,0), observer=(0,2,3)),
            make_event("b", direction=(0,1,0), observer=(10,-8,3)),
            make_event("c", direction=(0,0,1), observer=(10,2,-7)),
        )
        tracks = load_committed_tracklets(self.db)
        edges = propose_candidates(tracks, config())
        self.assertEqual(len(edges), 3)
        groups, used = form_consistent_groups(tracks, edges)
        self.assertEqual(sorted(map(len, groups)), [3])
        self.assertEqual(len(used), 3)
        world = GlobalTrackRepository(self.db).recompute(config(), "clique")
        self.assertEqual(world["associated_group_count"], 1)
        self.assertEqual(len(world["global_tracks"]), 1)
        identity = world["global_tracks"][0]
        self.assertEqual(identity["status"], "associated_group")
        self.assertEqual(identity["source_count"], 3)
        self.assertEqual(identity["fusion"], "NOT_FUSED")
        self.assertEqual(identity["class_name"], "tank")

    def test_nontransitive_evidence_cannot_merge_third_node(self):
        self.insert(
            make_event("a", direction=(1,0,0), observer=(0,2,3)),
            make_event("b", direction=(0,1,0), observer=(10,-8,3)),
            make_event("c", direction=(0,0,-1), observer=(10,2,-7)),
        )
        tracks = load_committed_tracklets(self.db)
        candidates = propose_candidates(tracks, config())
        self.assertEqual(len(candidates), 1)
        groups, used = form_consistent_groups(tracks, candidates)
        self.assertEqual(sorted(map(len, groups)), [1,2])
        self.assertEqual(len(used), 1)

    def test_invalid_tangent_covariance_not_claimed_as_evidence(self):
        raw = make_event("a", direction=(1,0,0), observer=(0,2,3))["spatial"]["bearing"]
        raw["tangent_covariance_rad2"] = [0,0,0,0]
        self.assertIsNone(read_ray(raw))


if __name__ == "__main__":
    unittest.main()
