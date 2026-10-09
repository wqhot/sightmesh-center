"""Deferred CI state-estimator regressions. DO NOT run until joint validation."""
from pathlib import Path
import json
import tempfile
import unittest

from sightmesh_center.association_v1 import AssociationPolicy
from sightmesh_center.fusion_ci import FusionPolicy, estimate_group
from sightmesh_center.global_tracks import GlobalTrackRepository
from sightmesh_center.track_inbox import DurableInbox
from sightmesh_center.tracklets import load_committed_tracklets


def policy(nodes=("a", "b", "c"), uncertainty_ns=1_000_000):
    return AssociationPolicy.from_dict({
        "sources": {
            name: {
                "verified": True,
                "domain": "shared_sim",
                "offset_ns": 0,
                "clock_uncertainty_ns": uncertainty_ns,
                "alignment_id": "verified-enu-2026",
                "coordinate_frame_id": "local_enu_v1",
                "map_revision": "map-rev-1",
            } for name in nodes
        },
        "class_labels": {name: {"0": "tank"} for name in nodes},
    })


def event(node, seq, x, time_ns=1_000_000_000,
          covariance=None, direction=None, observer=None):
    bearing = {"valid": False}
    if direction is not None:
        bearing = {
            "valid": True,
            "timestamp_ns": time_ns,
            "camera_position_world_m": list(observer),
            "direction_world": list(direction),
            "has_tangent_covariance": True,
            "tangent_covariance_rad2": [0.0001,0,0,0.0001],
        }
    return {
        "event_id": f"{node}-{seq}", "node_id": node,
        "camera_id": "camera0", "session_id": 10,
        "local_id": 1, "seq": seq,
        "type": "start" if seq == 1 else "update",
        "event_time_ns": time_ns, "frame_id": seq,
        "class_id": 0, "confidence": 0.9, "blobs": [],
        "spatial": {
            "world": {
                "valid": True, "coordinate_frame_id": "local_enu_v1",
                "x_m": x, "y_m": 2.0, "z_m": 3.0, "quality": 0.85,
                "position_covariance_m2": covariance or [1,0,0,0,1,0,0,0,1],
            },
            "localization_quality": {
                "map": {"valid": True, "revision": "map-rev-1"}
            },
            "bearing": bearing,
        },
    }


class ExperimentalFusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "inbox.sqlite"
        self.inbox = DurableInbox(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def insert(self, *events):
        for item in events:
            self.inbox.store_batch(json.dumps({
                "version": 1, "events": [item]
            }).encode("utf-8"))

    def fusion(self, pol=None):
        return estimate_group(load_committed_tracklets(self.db),
                              pol or policy(),
                              FusionPolicy.from_dict({}))

    def test_identical_correlated_positions_not_counted_as_independent(self):
        self.insert(event("a", 1, 10), event("b", 1, 10),
                    event("c", 1, 10))
        answer = self.fusion()
        self.assertTrue(answer["accepted"])
        cov = answer["estimate"]["position_covariance_m2"]
        # Naive information addition would shrink 1 m² to 1/3 m².
        # CI must retain approximately 1 m² (plus documented clock inflation).
        self.assertGreater(cov[0], 0.99)
        self.assertGreater(cov[4], 0.99)
        self.assertGreater(cov[8], 0.99)
        self.assertEqual(answer["estimate"]["event_time_domain"], "shared_sim")
        self.assertEqual(len(answer["estimate"]["weights"]), 3)

    def test_complementary_anisotropic_positions(self):
        self.insert(
            event("a", 1, 10,
                  covariance=[1,0,0,0,9,0,0,0,9]),
            event("b", 1, 10,
                  covariance=[9,0,0,0,1,0,0,0,9]),
        )
        result = self.fusion()
        self.assertTrue(result["accepted"])
        c = result["estimate"]["position_covariance_m2"]
        self.assertGreater(c[0], 1.0)
        self.assertLess(c[0], 9.0)
        self.assertGreater(c[4], 1.0)
        self.assertLess(c[4], 9.0)
        self.assertIn("not_a_guaranteed_bound",
                      result["estimate"]["covariance_qualification"])

    def test_opposing_calibrated_bearing_vetoes_ci_position(self):
        self.insert(
            event("a", 1, 10, direction=(1,0,0),
                  observer=(0,2,3)),
            event("b", 1, 10, direction=(0,-1,0),
                  observer=(10,-8,3)),
        )
        answer = self.fusion()
        self.assertFalse(answer["accepted"])
        self.assertEqual(answer["reason"], "bearing_geometry_contradiction")
        self.assertIn("b", answer["diagnostics"]["bearing_contradicting_sources"][0])

    def test_unobservable_bearing_does_not_shrink_ci_covariance(self):
        self.insert(
            event("a", 1, 10, direction=(1,0,0), observer=(0,2,3)),
            event("b", 1, 10, direction=(1,0,0), observer=(0,-8,3)),
        )
        answer = self.fusion()
        self.assertTrue(answer["accepted"])
        self.assertGreater(answer["estimate"]["position_covariance_m2"][0], 0.99)
        self.assertEqual(answer["diagnostics"]["bearing_policy"],
                         "veto_only_no_double_count")

    def test_unverified_clock_uncertainty_cannot_be_fused(self):
        self.insert(event("a", 1, 10), event("b", 1, 10))
        result = self.fusion(policy(uncertainty_ns=20_000_000))
        # This tight fusion policy is stricter than the association policy.
        self.assertTrue(result["accepted"])
        strict = FusionPolicy.from_dict({
            "max_clock_uncertainty_s": 0.005
        })
        rejected = estimate_group(load_committed_tracklets(self.db),
                                  policy(uncertainty_ns=20_000_000), strict)
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["reason"], "clock_uncertainty_unverified")

    def test_global_revision_tracks_fusion_policy_and_identity_split(self):
        self.insert(event("a", 1, 10), event("b", 1, 10))
        repo = GlobalTrackRepository(self.db)
        baseline = repo.recompute(policy(), "clique")
        self.assertEqual(baseline["fusion_mode"], "off")
        self.assertNotIn("fusion_estimate", baseline["global_tracks"][0])
        ci = FusionPolicy.from_dict({})
        fused = repo.recompute(policy(), "clique", fusion_policy=ci)
        self.assertGreater(fused["world_revision"], baseline["world_revision"])
        self.assertEqual(fused["experimental_fusion_accepted"], 1)
        self.assertEqual(fused["global_tracks"][0]["fusion"], "NOT_FUSED")
        self.assertIsNotNone(fused["global_tracks"][0]["fusion_estimate"])
        self.assertEqual(fused["global_tracks"][0]["fusion_estimate_status"],
                         "experimental_ci")
        again = repo.recompute(policy(), "clique", fusion_policy=ci)
        self.assertEqual(again["world_revision"], fused["world_revision"])

        # A late source update splits the previous identity; a previously
        # accepted estimate may not be carried into the new singleton.
        self.insert(event("b", 2, 200, time_ns=1_020_000_000))
        revised = repo.recompute(policy(), "clique", fusion_policy=ci)
        self.assertEqual(len(revised["global_tracks"]), 2)
        self.assertEqual(revised["experimental_fusion_accepted"], 0)
        self.assertTrue(all(track["fusion_estimate"] is None
                            for track in revised["global_tracks"]))
        self.assertGreater(revised["last_identity_revision"],
                           fused["last_identity_revision"])

    def test_invalid_ci_parameter_fails_closed(self):
        with self.assertRaises(ValueError):
            FusionPolicy.from_dict({"max_input_sigma_m": float("nan")})
        with self.assertRaises(ValueError):
            FusionPolicy.from_dict({"unknown_configuration": 2})


if __name__ == "__main__":
    unittest.main()
