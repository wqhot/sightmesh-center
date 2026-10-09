"""Deferred regression scenarios for optional SciPy fixed-lag factor estimation.

Do NOT run until joint validation. Synthetic inputs use the SAME JSON Bearing
shape emitted by Edge JsonProtocolCodec including tangent_basis_world.
"""
import math
import unittest

from sightmesh_center.association_v1 import AssociationPolicy
from sightmesh_center.tracklets import TrackKey, Observation, Tracklet
from sightmesh_center.window_factor_graph import WindowPolicy, estimate_window


def alignment(nodes):
    return AssociationPolicy.from_dict({
        "sources": {
            node: {
                "verified": True, "domain": "shared_sim",
                "alignment_id": "checked-common-origin",
                "coordinate_frame_id": "local_enu_v1",
                "map_revision": "map-v1",
                "offset_ns": 0, "clock_uncertainty_ns": 100000,
            } for node in nodes
        },
        "class_labels": {node: {"0": "tank"} for node in nodes},
    })


def ray_measurement(origin, target, when):
    diff = [target[i]-origin[i] for i in range(3)]
    norm = math.sqrt(sum(x*x for x in diff))
    d = [v/norm for v in diff]
    helper = (0,0,1) if abs(d[2]) < 0.9 else (1,0,0)
    first = [helper[1]*d[2]-helper[2]*d[1],
             helper[2]*d[0]-helper[0]*d[2],
             helper[0]*d[1]-helper[1]*d[0]]
    first_norm = math.sqrt(sum(v*v for v in first))
    first = [v/first_norm for v in first]
    second = [d[1]*first[2]-d[2]*first[1],
              d[2]*first[0]-d[0]*first[2],
              d[0]*first[1]-d[1]*first[0]]
    return {
        "valid": True,
        "timestamp_ns": when,
        "camera_position_world_m": list(origin),
        "direction_world": d,
        "tangent_basis_world": first+second,
        "has_tangent_covariance": True,
        "tangent_covariance_rad2": [1e-4,0,0,1e-4],
    }


def observed(node, i, timestamp_ns, camera, target,
             *, world_position=None, velocity=None):
    key = TrackKey(node, "camera0", 1, 1)
    world_cov = (1.0,0,0,0,1.0,0,0,0,1.0) if world_position else None
    observation = Observation(
        event_id=f"{node}-{i}",
        seq=i,
        event_ns=timestamp_ns,
        frame_id=i,
        class_id=0,
        position=world_position,
        covariance=world_cov,
        velocity=velocity,
        frame="local_enu_v1",
        map_revision="map-v1",
        quality=0.9,
        bearing=ray_measurement(camera, target, timestamp_ns))
    return Tracklet(key, 1, timestamp_ns, timestamp_ns,
                    observations=[observation], event_count=1)


def config(**overrides):
    return WindowPolicy.from_dict({
        "independent_source_errors_verified": True,
        "bearing_world_pose_frame_verified": True,
        "stationary_target_verified": True,
        "model": "static_position",
        "allow_single_position_anchor": False,
        **overrides
    })


class WindowFactorTests(unittest.TestCase):
    def _require_solver(self):
        try:
            import numpy  # noqa: F401
            import scipy  # noqa: F401
        except ImportError:
            self.skipTest("optional numpy/scipy not installed")

    def test_default_unverified_correlation_fails_closed(self):
        tracks = [
            observed("uav", 1, 1_000_000_000, (0,0,0), (10,5,3)),
            observed("ugv", 1, 1_000_000_000, (0,10,0), (10,5,3)),
        ]
        answer = estimate_window(tracks, alignment(["uav","ugv"]),
                                 WindowPolicy.from_dict({"model":"static_position"}))
        self.assertFalse(answer["accepted"])
        self.assertEqual(answer["reason"], "independent_source_errors_not_verified")

    def test_two_ray_static_triangulation_checks_rank_and_position(self):
        self._require_solver()
        truth=(10.0,5.0,3.0)
        tracks=[
            observed("uav", 1, 1_000_000_000, (0,0,0), truth),
            observed("ugv", 1, 1_000_000_000, (0,10,0), truth),
        ]
        answer=estimate_window(tracks, alignment(["uav","ugv"]), config())
        self.assertTrue(answer["accepted"], answer)
        for actual, expected in zip(answer["estimate"]["position_map_enu_m"], truth):
            self.assertAlmostEqual(actual, expected, places=5)
        self.assertEqual(answer["diagnostics"]["jacobian_rank"],3)
        self.assertEqual(answer["estimate"]["covariance_status"],
                         "UNCALIBRATED_CONDITIONAL_ONLY_NOT_PUBLISHABLE")

    def test_parallel_rays_refuse_to_invent_depth(self):
        self._require_solver()
        tracks=[
            observed("a", 1, 1_000_000_000, (0,0,0), (20,0,0)),
            observed("b", 1, 1_000_000_000, (1,0,0), (20,0,0))
        ]
        answer=estimate_window(tracks,alignment(["a","b"]),config())
        self.assertFalse(answer["accepted"])
        self.assertIn(answer["reason"], (
            "insufficient_cross_node_parallax",
            "unobservable_linear_geometry",
            "unobservable_or_ill_conditioned_state"))

    def test_constant_velocity_observability_rejects_simultaneous_bearings(self):
        self._require_solver()
        nodes=["a","b","c"]
        cameras=[(0,0,0),(0,10,0),(10,0,0)]
        tracks=[observed(n,1,1_000_000_000,c,(10,5,3))
                for n,c in zip(nodes,cameras)]
        answer=estimate_window(tracks,alignment(nodes),config(
            model="constant_velocity"))
        self.assertFalse(answer["accepted"])
        self.assertEqual(answer["reason"],
                         "insufficient_temporal_baseline_for_velocity")

    def test_constant_velocity_four_nodes_time_diversity(self):
        self._require_solver()
        nodes=["a","b","c","d"]
        cameras=[(0,0,0),(20,0,0),(0,20,3),(0,0,20)]
        velocity=(1.0,-0.5,0.4)
        dt=[0,0.2,0.4,0.6]
        times=[1_000_000_000+round(t*1e9) for t in dt]
        tracks=[
            observed(node,1,t_ns,cam,tuple(
                (10,5,3)[k]+velocity[k]*sec for k in range(3)))
            for node,t_ns,cam,sec in zip(nodes,times,cameras,dt)]
        answer=estimate_window(tracks,alignment(nodes),config(
            model="constant_velocity", max_condition_number=1e7))
        self.assertTrue(answer["accepted"], answer)
        pred=answer["estimate"]["velocity_map_enu_mps"]
        for x,y in zip(pred,velocity):
            self.assertAlmostEqual(x,y,delta=0.05)
        self.assertEqual(answer["diagnostics"]["state_dimension"],6)

    def test_same_node_bearing_not_double_counted_with_position_anchor(self):
        self._require_solver()
        truth=(10.0,5.0,3.0)
        tracks=[
            observed("uav",1,1_000_000_000,(0,0,0),truth,
                     world_position=truth),
            observed("ugv",1,1_000_000_000,(0,10,0),truth)
        ]
        answer=estimate_window(tracks, alignment(["uav","ugv"]), config(
            allow_single_position_anchor=True))
        self.assertTrue(answer["accepted"],answer)
        self.assertEqual(answer["diagnostics"]["position_anchor_tracklet"],
                         tracks[0].uid)
        self.assertEqual(answer["diagnostics"]["bearing_nodes"],["ugv"])

    def test_independent_temporal_error_gate_caps_same_node_samples(self):
        self._require_solver()
        truth=(10.0,5.0,3.0)
        first=observed("uav",1,1_000_000_000,(0,0,0),truth)
        later=observed("uav",2,1_100_000_000,(0,0.2,0),truth)
        first.observations.extend(later.observations)
        first.end_ns=1_100_000_000
        second=observed("ugv",1,1_100_000_000,(0,10,0),truth)
        answer=estimate_window([first,second],alignment(["uav","ugv"]),config())
        self.assertTrue(answer["accepted"], answer)
        self.assertEqual(answer["diagnostics"]["bearing_factor_count"],2)

    def test_stale_bearing_or_invalid_frame_fails_without_solution(self):
        self._require_solver()
        truth=(10.0,5.0,3.0)
        tracks=[
            observed("uav",1,1_000_000_000,(0,0,0),truth),
            observed("ugv",1,1_000_000_000,(0,10,0),truth)]
        tracks[0].observations[0].bearing["timestamp_ns"]=1_600_000_000
        answer=estimate_window(tracks,alignment(["uav","ugv"]),config())
        self.assertFalse(answer["accepted"])
        self.assertEqual(answer["reason"],"insufficient_cross_node_bearing_factors")


if __name__=="__main__":
    unittest.main()
