"""Experimental fixed-lag bearing/position factor estimator.

Only works on existing identity groups, not a global data-association solver.
Uses optional NumPy + SciPy least_squares. No positional results are
automatically promoted into the published GlobalTrack state.

Critical statistical limitations:
* Edge localization may already use its own Bearing: never use position and
  Bearing from the same physical node in one solve.
* Across nodes, shared map/camera pose/clock errors create correlation. The
  operator MUST independently verify the error model before enabling this.
* Multiple rays from one node across time require a separate verified
  temporal-independence assertion; otherwise only one ray per node is used.
* The reported normal-matrix covariance is CONDITIONAL/UNCALIBRATED, not
  a validated NEES coverage bound. No process-noise-free Kalman recursion.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import math
from typing import Any

from .association_v1 import AssociationPolicy
from .tracklets import Tracklet


@dataclass(frozen=True)
class WindowPolicy:
    model: str = "constant_velocity"         # or static_position
    window_seconds: float = 1.5
    min_time_baseline_seconds: float = 0.15
    min_ray_intersection_degrees: float = 4.0
    max_clock_uncertainty_seconds: float = 0.03
    max_bearing_timestamp_delta_seconds: float = 0.2
    max_condition_number: float = 1_000_000.0
    max_normalized_rms: float = 4.0
    max_target_speed_mps: float = 80.0
    max_observations: int = 64
    independent_source_errors_verified: bool = False
    independent_temporal_errors_verified: bool = False
    bearing_world_pose_frame_verified: bool = False
    stationary_target_verified: bool = False
    allow_single_position_anchor: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WindowPolicy":
        if type(raw) is not dict:
            raise ValueError("window_factor_graph must be an object")
        known = {f.name for f in fields(cls)}
        if set(raw) - known:
            raise ValueError("unknown window factor parameter")
        params = {f.name: raw.get(f.name, getattr(cls, f.name)) for f in fields(cls)}
        if params["model"] not in ("constant_velocity", "static_position"):
            raise ValueError("unsupported window motion model")
        ranges = {
            "window_seconds": (0.05, 5.0),
            "min_time_baseline_seconds": (0.01, 3.0),
            "min_ray_intersection_degrees": (0.1, 45.0),
            "max_clock_uncertainty_seconds": (0.000001, 0.2),
            "max_bearing_timestamp_delta_seconds": (0.001, 1.0),
            "max_condition_number": (10.0, 1e9),
            "max_normalized_rms": (0.1, 30.0),
            "max_target_speed_mps": (0.1, 300.0),
        }
        for key, (low, high) in ranges.items():
            x = params[key]
            if type(x) not in (int, float) or not math.isfinite(x) or not low <= x <= high:
                raise ValueError("invalid window parameter " + key)
        if params["min_time_baseline_seconds"] >= params["window_seconds"]:
            raise ValueError("time baseline must be shorter than window")
        if type(params["max_observations"]) is not int or not 4 <= params["max_observations"] <= 256:
            raise ValueError("invalid max_observations")
        for key in ("independent_source_errors_verified",
                    "independent_temporal_errors_verified",
                    "bearing_world_pose_frame_verified",
                    "stationary_target_verified",
                    "allow_single_position_anchor"):
            if type(params[key]) is not bool:
                raise ValueError("invalid boolean window gate " + key)
        return cls(**params)


def _refuse(reason: str, **diagnostics: Any) -> dict[str, Any]:
    return {"accepted": False, "reason": reason, "diagnostics": diagnostics}


def _numeric(raw: Any, count: int):
    if type(raw) not in (list, tuple) or len(raw) != count or any(
        type(v) not in (int, float) or not math.isfinite(v) for v in raw
    ):
        return None
    return tuple(float(v) for v in raw)


def _validated_ray(raw: Any):
    """Validate direction and Edge's actual 2x3 tangent basis, plus SPD 2x2."""
    if not isinstance(raw, dict) or raw.get("valid") is not True or \
            raw.get("has_tangent_covariance") is not True:
        return None
    origin = _numeric(raw.get("camera_position_world_m"), 3)
    direction = _numeric(raw.get("direction_world"), 3)
    basis = _numeric(raw.get("tangent_basis_world"), 6)
    covariance = _numeric(raw.get("tangent_covariance_rad2"), 4)
    timestamp = raw.get("timestamp_ns")
    if origin is None or direction is None or basis is None or covariance is None \
            or type(timestamp) is not int or timestamp <= 0:
        return None
    norm = math.sqrt(sum(x*x for x in direction))
    if not 0.99 <= norm <= 1.01:
        return None
    direction = tuple(x/norm for x in direction)
    e1, e2 = basis[:3], basis[3:]
    for e in (e1, e2):
        if abs(sum(x*x for x in e)-1) > 0.03 or \
                abs(sum(e[i]*direction[i] for i in range(3))) > 0.03:
            return None
    if abs(sum(e1[i]*e2[i] for i in range(3))) > 0.03:
        return None
    a, b, c, d = covariance
    if a <= 1e-10 or d <= 1e-10 or max(a, d) > 0.25 or \
            abs(b-c) > 1e-8 or a*d-b*b <= 1e-14:
        return None
    return origin, direction, (e1, e2), ((a,b),(c,d)), timestamp


def estimate_window(group: list[Tracklet], align: AssociationPolicy,
                    cfg: WindowPolicy) -> dict[str, Any]:
    """Conservative fixed-window factor optimization for known track identities.

    Returns a diagnostic sidecar only. No fallback to pseudo-inverse/zero-prior
    if full rank, geometric baseline, time span or source independence fails.
    """
    if len(group) < 2 or len(group) > 8:
        return _refuse("requires_preassociated_multi_node_group")
    if not cfg.independent_source_errors_verified:
        return _refuse("independent_source_errors_not_verified")
    if not cfg.bearing_world_pose_frame_verified:
        return _refuse("bearing_camera_pose_frame_not_verified")
    if cfg.model == "static_position" and not cfg.stationary_target_verified:
        return _refuse("static_motion_model_not_verified")
    try:
        import numpy as np
        from scipy.optimize import least_squares
    except ImportError:
        return _refuse("optional_numpy_scipy_unavailable")

    source_context = None
    names = set()
    collected = []
    for tracklet in sorted(group, key=lambda t: t.uid):
        node = tracklet.key.node
        source = align.sources.get(node)
        if source is None:
            return _refuse("unverified_source", node=node)
        semantic = align.class_labels.get(node, {}).get(tracklet.class_id)
        ctx = (source.domain, source.alignment_id,
               source.coordinate_frame_id, source.map_revision, semantic)
        if not semantic or (source_context is not None and ctx != source_context):
            return _refuse("unverified_class_clock_or_map")
        source_context = ctx
        if node in names:
            return _refuse("duplicate_physical_node")
        names.add(node)
        if source.clock_uncertainty_ns/1e9 > cfg.max_clock_uncertainty_seconds:
            return _refuse("clock_uncertainty_exceeds_limit", node=node)
        for obs in tracklet.observations:
            # Source-relative time is explicitly remapped to a common domain.
            # Edge-bearing timestamp need not be identical to event_time_ns.
            br = _validated_ray(obs.bearing)
            bearing = None
            if br is not None:
                t_b = br[4] + source.offset_ns
                if abs(br[4] - obs.event_ns)/1e9 <= cfg.max_bearing_timestamp_delta_seconds:
                    bearing = (t_b, br, obs.event_id, obs.seq)
            position = None
            if (obs.position is not None and obs.covariance is not None and
                obs.frame == source.coordinate_frame_id and
                obs.map_revision == source.map_revision and
                obs.quality >= align.min_quality):
                position = (obs.event_ns + source.offset_ns,
                            obs.position, obs.covariance, obs.event_id, obs.seq)
            if bearing is not None or position is not None:
                collected.append((tracklet, source, position, bearing))

    if not collected:
        return _refuse("no_valid_observations")
    all_times = ([entry[2][0] for entry in collected if entry[2] is not None] +
                 [entry[3][0] for entry in collected if entry[3] is not None])
    cutoff = max(all_times) - round(cfg.window_seconds * 1e9)

    # One position anchor MAX across all nodes and times. Position and Bearing
    # may have unknown common errors even within a single Edge track.
    anchor_candidates = sorted(
        ((t, src, position) for t, src, position, _ in collected
         if position is not None and position[0] >= cutoff),
        key=lambda x: (sum(x[2][2][k] for k in (0,4,8)),
                       -x[2][0], x[0].uid))
    anchor = anchor_candidates[0] if (cfg.allow_single_position_anchor and
                                       anchor_candidates) else None
    anchor_node = anchor[0].key.node if anchor else None
    bearings = sorted(
        ((t, src, ray) for t, src, _, ray in collected
         if ray is not None and ray[0] >= cutoff and t.key.node != anchor_node),
        key=lambda x: (x[2][0], x[0].uid, x[2][3]))
    if not cfg.independent_temporal_errors_verified:
        # One ray per node to avoid treating correlated video frames as
        # independent. Choose the most recent in the window.
        last = {}
        for record in bearings:
            last[record[0].key.node] = record
        bearings = sorted(last.values(), key=lambda x: (x[2][0], x[0].uid))
    if len(bearings) + (1 if anchor else 0) > cfg.max_observations:
        return _refuse("window_observation_limit_exceeded")
    if len(bearings) < (1 if anchor else 2):
        return _refuse("insufficient_cross_node_bearing_factors")

    origins = np.array([b[2][1][0] for b in bearings], dtype=float)
    directions = np.array([b[2][1][1] for b in bearings], dtype=float)
    angles = [math.degrees(math.acos(min(1.0, max(-1.0,
              abs(float(np.dot(directions[i], directions[j])))))))
              for i in range(len(bearings)) for j in range(i+1,len(bearings))
              if bearings[i][0].key.node != bearings[j][0].key.node]
    if anchor is None and (not angles or max(angles) < cfg.min_ray_intersection_degrees):
        return _refuse("insufficient_cross_node_parallax", maximum_angle_deg=max(angles, default=0))
    if anchor is not None:
        # A single anchor and same-direction rays may be mathematically
        # sufficient for position but not meaningful cross-node geometry.
        ray_to_anchor = anchor[2][1]
        for b in bearings:
            origin, direction = b[2][1][0:2]
            v = np.asarray(ray_to_anchor)-np.asarray(origin)
            distance = float(np.linalg.norm(v))
            if distance < 0.01:
                return _refuse("anchor_at_ray_origin")
            if float(np.dot(v/distance, direction)) <= 0:
                return _refuse("anchor_behind_bearing_ray")
        if not angles and len(bearings) < 2:
            # Retain an explicit single ray+anchor mode; the rank test below
            # independently determines whether velocity is observable.
            pass

    selected_times = [r[2][0] for r in bearings]
    if anchor is not None:
        selected_times.append(anchor[2][0])
    epoch = max(selected_times)
    if epoch < 0:
        return _refuse("invalid_common_time_epoch")
    span = (max(selected_times)-min(selected_times))/1e9
    duration = cfg.window_seconds
    dynamic = cfg.model == "constant_velocity"
    dim = 6 if dynamic else 3
    if dynamic and span < cfg.min_time_baseline_seconds:
        return _refuse("insufficient_temporal_baseline_for_velocity", window_span_s=span)

    # Linear geometric seed from perpendicular ray projectors, at each
    # measurement time. The Jacobian of these equations is well-defined at
    # the start; if it is rank deficient do not invent a zero-velocity prior.
    rows = []
    rhs = []
    for _, _, ray in bearings:
        t_ns, br = ray[0], ray[1]
        o, d, basis = np.asarray(br[0]), np.asarray(br[1]), br[2]
        dt = (t_ns-epoch)/1e9
        for tangent in basis:
            e = np.asarray(tangent)
            rows.append(np.r_[e, e*(dt/duration)] if dynamic else e)
            rhs.append(float(e@o))
    if anchor is not None:
        _, _, (t_ns, p, covariance, _, _) = anchor
        dt = (t_ns-epoch)/1e9
        for axis in range(3):
            e = np.eye(3)[axis]
            rows.append(np.r_[e,e*(dt/duration)] if dynamic else e)
            rhs.append(p[axis])
    initial_matrix = np.asarray(rows)
    if initial_matrix.shape[0] < dim or int(np.linalg.matrix_rank(initial_matrix,
             tol=1e-8)) < dim:
        return _refuse("unobservable_linear_geometry", rank=int(
            np.linalg.matrix_rank(initial_matrix, tol=1e-8)), state_dimension=dim)
    x0, _, _, _ = np.linalg.lstsq(initial_matrix, np.asarray(rhs), rcond=None)
    if not np.all(np.isfinite(x0)):
        return _refuse("invalid_initial_state")

    factors = []
    bearing_nodes = set()
    for tracklet, source, ray in bearings:
        timestamp, br, event_id, seq = ray
        o, d, (e1, e2), covariance, _ = br
        L = np.linalg.cholesky(np.asarray(covariance, dtype=float))
        factors.append(("bearing", tracklet.uid, event_id, timestamp,
                        np.asarray(o), np.vstack([e1,e2]), L, np.asarray(d)))
        bearing_nodes.add(tracklet.key.node)
    if anchor is not None:
        t, _, (timestamp, p, covariance, event_id, seq) = anchor
        L = np.linalg.cholesky(np.asarray(covariance).reshape(3,3))
        factors.append(("position", t.uid, event_id, timestamp,
                        np.asarray(p), None, L, None))

    # Bearing angular residual in the measured tangent frame:
    # r = L^-1 * [e1·(x-o), e2·(x-o)] / ||x-o||
    # Position residual r = L^-1 * (x - p_meas).
    # Do not include position and bearing from the same source; no hidden prior.
    def residual(state):
        p = state[:3]
        v_scaled = state[3:] if dynamic else None
        result = []
        for kind, uid, event_id, timestamp, data, basis, L, direction in factors:
            dt_norm = (timestamp-epoch)/1e9/duration
            position = p + v_scaled*dt_norm if dynamic else p
            if kind == "bearing":
                relative = position-data
                norm = max(float(np.linalg.norm(relative)), 1e-6)
                projection = float(relative @ direction)
                tangent_residual = basis @ relative / norm
                # Do not turn behind-camera projections into apparently good
                # tangent residuals; reject after fit using forward-ray gate.
                result.extend(np.linalg.solve(L, tangent_residual).tolist())
            else:
                result.extend(np.linalg.solve(L, position-data).tolist())
        return np.array(result)

    try:
        result = least_squares(residual, x0, method="trf", loss="linear",
                               max_nfev=120, xtol=1e-10,
                               ftol=1e-10, gtol=1e-10)
    except (ValueError, FloatingPointError, np.linalg.LinAlgError):
        return _refuse("optimization_numerical_failure")
    if not result.success or not np.all(np.isfinite(result.x)) or \
            not np.all(np.isfinite(result.jac)):
        return _refuse("nonconvergent_window_optimization",
                       optimizer_status=int(result.status))
    singular = np.linalg.svd(result.jac, compute_uv=False)
    if singular.size != dim:
        return _refuse("unobservable_optimized_state")
    largest, smallest = float(singular[0]), float(singular[-1])
    condition = largest/smallest if smallest > 0 else float("inf")
    if smallest <= 1e-7 or not math.isfinite(condition) or \
            condition > cfg.max_condition_number:
        return _refuse("unobservable_or_ill_conditioned_state",
                       rank=int(np.linalg.matrix_rank(result.jac, tol=1e-7)),
                       condition_number=condition)
    rms = float(math.sqrt(float(result.fun @ result.fun)/len(result.fun)))
    if not math.isfinite(rms) or rms > cfg.max_normalized_rms:
        return _refuse("factor_residual_exceeds_gate", normalized_rms=rms)

    pos = result.x[:3]
    velocity = result.x[3:]/duration if dynamic else None
    if velocity is not None and float(np.linalg.norm(velocity)) > cfg.max_target_speed_mps:
        return _refuse("unphysical_estimated_velocity")
    forward_residuals = []
    for kind, uid, event_id, t_ns, origin, basis, L, direction in factors:
        if kind != "bearing":
            continue
        pred = pos + velocity*((t_ns-epoch)/1e9) if dynamic else pos
        if float((pred-origin)@direction) <= 0.01:
            forward_residuals.append(event_id)
    if forward_residuals:
        return _refuse("ray_points_behind_camera", events=forward_residuals)

    # Local information-matrix inverse is only a CONDITIONAL curvature
    # diagnostic. Camera/extrinsic uncertainty and cross-source correlations
    # are not marginalized; it is not a calibrated physical covariance.
    normal = result.jac.T @ result.jac
    conditional_covariance = np.linalg.inv(normal)
    if dynamic:
        scale = np.diag([1,1,1,1/duration,1/duration,1/duration])
        conditional_covariance = scale @ conditional_covariance @ scale.T
    if not np.all(np.isfinite(conditional_covariance)):
        return _refuse("invalid_conditional_curvature")

    return {
        "accepted": True,
        "reason": "conditional_fixed_lag_solution_requires_validation",
        "estimate": {
            "model": cfg.model,
            "position_map_enu_m": pos.tolist(),
            "velocity_map_enu_mps": velocity.tolist() if dynamic else None,
            "epoch_time_ns": epoch,
            "clock_domain": source_context[0],
            "alignment_id": source_context[1],
            "coordinate_frame_id": source_context[2],
            "map_revision": source_context[3],
            "class_name": source_context[4],
            "conditional_information_covariance": conditional_covariance.tolist(),
            "covariance_status": "UNCALIBRATED_CONDITIONAL_ONLY_NOT_PUBLISHABLE",
            "not_a_joint_global_track_state": True,
        },
        "diagnostics": {
            "state_dimension": dim,
            "jacobian_rank": dim,
            "jacobian_condition_number": condition,
            "normalized_rms": rms,
            "sample_time_span_s": span,
            "bearing_factor_count": len(bearings),
            "bearing_nodes": sorted(bearing_nodes),
            "position_anchor_tracklet": anchor[0].uid if anchor else None,
            "factor_event_ids": sorted({f[2] for f in factors}),
            "window_seconds": duration,
            "independence_gate": "explicit_operator_verified_only",
            "covariance_limitation": "uncalibrated_camera_pose_motion_clock_and_cross_correlation",
        },
    }
