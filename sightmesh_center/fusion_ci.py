"""Experimental conservative Center position fusion with bearing *validation*.

The algorithm fuses at most one verified 3D position/covariance from each
physical Edge node using sequential Covariance Intersection (CI). Input
cross-correlation is UNKNOWN, so Kalman/inverse-covariance summation would be
incorrect. Bearing observations are additional geometric consistency VETOES,
not independent measurements added to the CI information matrix: the same
Edge localization might already have consumed those bearings.

Clock-domain alignment and physical motion uncertainty are explicit. The
clock/motion term is a documented isotropic heuristic variance inflation,
NOT a rigorously calibrated probabilistic bound. The result must remain
experimental until real-data NEES/NIS + calibration validation passes.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Optional

from .association_v1 import AssociationPolicy
from .bearing_geometry import read_ray
from .tracklets import Tracklet


Vec = tuple[float, float, float]
Mat = tuple[float, ...]


@dataclass(frozen=True)
class FusionPolicy:
    max_epoch_offset_s: float = 0.25
    max_clock_uncertainty_s: float = 0.05
    motion_sigma_speed_mps: float = 30.0
    max_input_sigma_m: float = 15.0
    max_residual_d2: float = 25.0
    max_bearing_residual_sigma: float = 4.0
    max_bearing_sample_age_s: float = 0.25
    min_position_sources: int = 2

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "FusionPolicy":
        if type(data) is not dict:
            raise ValueError("fusion policy must be a JSON object")
        bounds = {
            "max_epoch_offset_s": (0.001, 1),
            "max_clock_uncertainty_s": (0.000001, 1),
            "motion_sigma_speed_mps": (0.1, 200),
            "max_input_sigma_m": (0.001, 100),
            "max_residual_d2": (0.1, 200),
            "max_bearing_residual_sigma": (0.1, 20),
            "max_bearing_sample_age_s": (0.001, 1),
        }
        params: dict[str, Any] = {}
        for name, (minimum, maximum) in bounds.items():
            value = data.get(name, getattr(FusionPolicy, name))
            if type(value) not in (float, int) or not math.isfinite(value) \
                    or not minimum <= value <= maximum:
                raise ValueError(f"fusion.{name} outside [{minimum},{maximum}]")
            params[name] = float(value)
        n = data.get("min_position_sources", 2)
        if type(n) is not int or not 2 <= n <= 4:
            raise ValueError("fusion.min_position_sources must be 2..4")
        params["min_position_sources"] = n
        if set(data) - (set(params) | {"method"}):
            raise ValueError("unknown fusion policy field(s)")
        if data.get("method", "ci_position_bearing_veto") != "ci_position_bearing_veto":
            raise ValueError("only experimental ci_position_bearing_veto is supported")
        return FusionPolicy(**params)


def _transpose(a: Mat) -> Mat:
    return tuple(a[j*3+i] for i in range(3) for j in range(3))


def _quadratic(v: Vec, mat: Mat) -> float:
    return sum(v[i] * mat[i*3+j] * v[j]
               for i in range(3) for j in range(3))


def _matvec(a: Mat, x: Vec) -> Vec:
    return tuple(sum(a[i*3+j] * x[j] for j in range(3))
                 for i in range(3))  # type: ignore[return-value]


def _invert_spd(a: Mat) -> tuple[Mat, float] | None:
    """Inverse plus log(det), explicitly reject non-SPD/ill-conditioned input."""
    if len(a) != 9 or any(not math.isfinite(x) for x in a):
        return None
    for i in range(3):
        for j in range(i):
            if abs(a[i*3+j] - a[j*3+i]) > 1e-7 * max(
                    1.0, abs(a[i*3+j]), abs(a[j*3+i])):
                return None
    L = [[0.0]*3 for _ in range(3)]
    for i in range(3):
        for j in range(i+1):
            entry = a[i*3+j] - sum(L[i][k]*L[j][k] for k in range(j))
            if i == j:
                if entry <= 1e-12 or not math.isfinite(entry):
                    return None
                L[i][j] = math.sqrt(entry)
            else:
                L[i][j] = entry/L[j][j]
    if min(L[i][i] for i in range(3)) / max(L[i][i] for i in range(3)) < 1e-6:
        return None
    result = [[0.0]*3 for _ in range(3)]
    for col in range(3):
        y = [0.0]*3
        for i in range(3):
            y[i] = ((1.0 if i == col else 0.0) -
                    sum(L[i][k]*y[k] for k in range(i))) / L[i][i]
        x = [0.0]*3
        for i in reversed(range(3)):
            x[i] = (y[i] - sum(L[k][i]*x[k] for k in range(i+1,3))) / L[i][i]
        for i in range(3):
            result[i][col] = x[i]
    # Symmetrize only numerical roundoff, NOT a physically asymmetric matrix.
    inv = tuple((result[i][j]+result[j][i])*0.5
                for i in range(3) for j in range(3))
    if any(not math.isfinite(v) for v in inv):
        return None
    return inv, 2*sum(math.log(L[i][i]) for i in range(3))


def _ci_pair(
    p: Vec, cov_p: Mat, q: Vec, cov_q: Mat,
) -> tuple[Vec, Mat, float] | None:
    inv_a = _invert_spd(cov_p)
    inv_b = _invert_spd(cov_q)
    if inv_a is None or inv_b is None:
        return None
    I, J = inv_a[0], inv_b[0]
    def candidate(alpha: float):
        information = tuple(alpha*I[k] + (1-alpha)*J[k] for k in range(9))
        inverse = _invert_spd(information)
        return inverse, information

    # Maximize log det of the information matrix (minimize CI log-det).
    # In alpha the log-det is concave; golden section is deterministic.
    low, high = 0.0, 1.0
    phi = (math.sqrt(5)-1)/2
    x = high-phi*(high-low)
    y = low+phi*(high-low)
    def objective(alpha):
        inverse, _ = candidate(alpha)
        return inverse[1] if inverse is not None else -float("inf")
    fx, fy = objective(x), objective(y)
    for _ in range(38):
        if fx < fy:
            low, x, fx = x, y, fy
            y = low+phi*(high-low)
            fy = objective(y)
        else:
            high, y, fy = y, x, fx
            x = high-phi*(high-low)
            fx = objective(x)
    alpha = max((0.0, 1.0, (low+high)/2),
                key=lambda z: objective(z))
    inverse, information = candidate(alpha)
    if inverse is None:
        return None
    covariance = inverse[0]
    lhs, rhs = _matvec(I, p), _matvec(J, q)
    weighted = tuple(alpha*lhs[k]+(1-alpha)*rhs[k] for k in range(3))
    mean = _matvec(covariance, weighted)
    if not all(math.isfinite(x) for x in mean):
        return None
    return mean, covariance, alpha


def estimate_group(
    tracklets: list[Tracklet],
    alignment: AssociationPolicy,
    config: FusionPolicy,
) -> dict[str, Any]:
    """Return {accepted, reason, estimate?, diagnostics} with no side effects."""
    reason = "not_enough_verified_position_sources"
    if len(tracklets) < config.min_position_sources:
        return {"accepted": False, "reason": reason, "diagnostics": {}}

    sources = []
    context = None
    for tracklet in sorted(tracklets, key=lambda t: t.uid):
        source = alignment.sources.get(tracklet.key.node)
        obs = tracklet.latest
        if source is None or obs is None or obs.position is None or obs.covariance is None:
            return {"accepted": False, "reason": "source_unverified_or_unlocalized",
                    "diagnostics": {}}
        class_name = alignment.class_labels.get(tracklet.key.node, {}).get(
            tracklet.class_id)
        if not class_name:
            return {"accepted": False, "reason": "semantic_class_unverified",
                    "diagnostics": {}}
        attrs = (source.domain, source.alignment_id, source.coordinate_frame_id,
                 source.map_revision, class_name)
        if context is None:
            context = attrs
        if attrs != context or obs.frame != source.coordinate_frame_id \
                or obs.map_revision != source.map_revision:
            return {"accepted": False, "reason": "clock_map_or_class_mismatch",
                    "diagnostics": {}}
        if obs.quality < alignment.min_quality:
            return {"accepted": False, "reason": "source_localization_quality_below_gate",
                    "diagnostics": {}}
        if source.clock_uncertainty_ns/1e9 > config.max_clock_uncertainty_s:
            return {"accepted": False, "reason": "clock_uncertainty_unverified",
                    "diagnostics": {}}
        if any(obs.covariance[i] > config.max_input_sigma_m**2 for i in (0,4,8)):
            return {"accepted": False, "reason": "covariance_outside_bounds",
                    "diagnostics": {}}
        if _invert_spd(obs.covariance) is None:
            return {"accepted": False, "reason": "covariance_not_spd",
                    "diagnostics": {}}
        sources.append((tracklet, source, obs,
                        obs.event_ns + source.offset_ns))
    if len({item[0].key.node for item in sources}) != len(sources):
        return {"accepted": False, "reason": "duplicate_physical_node",
                "diagnostics": {}}

    # Evaluate all observations at a deterministic common source-time epoch:
    # middle of the ordered aligned timestamps. Source times, NOT Center HTTP
    # arrival nor WallClock, are the only physical temporal quantities here.
    times = sorted(item[3] for item in sources)
    epoch = times[len(times)//2] if len(times)%2 else (times[
        len(times)//2-1]+times[len(times)//2])//2
    if epoch < 0:
        return {"accepted": False, "reason": "aligned_event_time_negative",
                "diagnostics": {"epoch_time_ns": epoch}}
    positions = []
    for tracklet, source, obs, timestamp in sources:
        dt = (epoch-timestamp)/1e9
        unc = source.clock_uncertainty_ns/1e9
        if abs(dt) > config.max_epoch_offset_s:
            return {"accepted": False, "reason": "time_extrapolation_limit",
                    "diagnostics": {"epoch_time_ns": epoch}}
        # x(t)=x0+v*dt is optional; a velocity without covariance cannot
        # be treated as exact. Add deterministic motion uncertainty in P.
        xyz = tuple(obs.position[i] +
                    ((obs.velocity[i]*dt) if obs.velocity else 0.0)
                    for i in range(3))
        inflation = (config.motion_sigma_speed_mps*(abs(dt)+unc))**2
        cov = tuple(obs.covariance[k] +
                    (inflation if k in (0,4,8) else 0.0) for k in range(9))
        if _invert_spd(cov) is None:
            return {"accepted": False, "reason": "inflated_covariance_not_spd",
                    "diagnostics": {}}
        positions.append((tracklet, source, obs, xyz, cov, unc, abs(dt)))

    mean, covariance = positions[0][3], positions[0][4]
    weights = {positions[0][0].uid: 1.0}
    for tracklet, _, _, pos, cov, _, _ in positions[1:]:
        combined = _ci_pair(mean, covariance, pos, cov)
        if combined is None:
            return {"accepted": False, "reason": "ci_numerical_degeneracy",
                    "diagnostics": {}}
        mean, covariance, alpha = combined
        weights = {uid: weight*alpha for uid, weight in weights.items()}
        weights[tracklet.uid] = 1-alpha
    # Reject contradictions rather than produce a deceptively precise CI
    # solution from physically incompatible source estimates.
    residuals = {}
    for tracklet, _, _, pos, cov, _, _ in positions:
        inv = _invert_spd(cov)
        if inv is None:
            return {"accepted": False, "reason": "source_covariance_invalid",
                    "diagnostics": {}}
        diff = tuple(mean[i]-pos[i] for i in range(3))
        d2 = _quadratic(diff, inv[0])
        if not math.isfinite(d2) or d2 > config.max_residual_d2:
            return {"accepted": False, "reason": "position_residual_exceeds_gate",
                    "diagnostics": {"max_position_residual_d2": d2}}
        residuals[tracklet.uid] = d2

    bearing_count = 0
    bearing_max = 0.0
    bearing_rejected = []
    for tracklet, source, obs, _, _, unc, dt in positions:
        raw = obs.bearing
        if type(raw) is not dict or type(raw.get("timestamp_ns")) is not int:
            continue
        age = abs(raw["timestamp_ns"]-obs.event_ns)/1e9
        if age > config.max_bearing_sample_age_s:
            continue
        ray = read_ray(raw)
        if ray is None:
            continue
        v = tuple(mean[k]-ray.origin[k] for k in range(3))
        range_m = sum(v[k]*ray.direction[k] for k in range(3))
        if range_m <= 0.05:
            bearing_rejected.append(tracklet.uid)
            continue
        perp = tuple(v[k]-range_m*ray.direction[k] for k in range(3))
        displacement = math.sqrt(sum(x*x for x in perp))
        # A bearing may be ALREADY incorporated in Edge's 3D localization;
        # it may only VETO inconsistent joint positions, NEVER tighten Pci.
        # Use Pci largest coordinate variance plus angular/time terms as a
        # conservative diagnostic scale, not a claimed calibrated likelihood.
        scale = math.sqrt(max(covariance[i] for i in (0,4,8)) +
                          (range_m*ray.angular_sigma_rad)**2 +
                          (config.motion_sigma_speed_mps*(dt+unc+age))**2)
        normalized = displacement/max(scale,1e-6)
        bearing_count += 1
        bearing_max = max(bearing_max, normalized)
        if normalized > config.max_bearing_residual_sigma:
            bearing_rejected.append(tracklet.uid)
    diagnostics = {
        "epoch_time_ns": epoch,
        "position_residual_d2": residuals,
        "bearing_factor_count": bearing_count,
        "bearing_normalized_residual_max": bearing_max,
        "bearing_contradicting_sources": bearing_rejected,
        "weights": weights,
        "input_source_count": len(positions),
        "bearing_policy": "veto_only_no_double_count",
        "clock_motion_uncertainty": "heuristic_isotropic_variance_not_calibrated",
    }
    if bearing_rejected:
        return {"accepted": False, "reason": "bearing_geometry_contradiction",
                "diagnostics": diagnostics}

    return {"accepted": True, "reason": "ci_position_only",
            "estimate": {
                "method": "COVARIANCE_INTERSECTION",
                "state_type": "POSITION_ONLY",
                "source": "ci_unknown_cross_correlation_bearing_veto",
                "position_map_enu_m": list(mean),
                "position_covariance_m2": list(covariance),
                "event_time_ns": epoch,  # aligned source epoch, not Unix if simulation
                "event_time_domain": context[0],
                "coordinate_frame_id": context[2],
                "map_revision": context[3],
                "alignment_id": context[1],
                "input_tracklets": [item[0].uid for item in positions],
                "weights": weights,
                "covariance_qualification":
                    "CI_conditional_on_consistent_calibrated_inputs_"
                    "with_heuristic_motion_inflation_not_a_guaranteed_bound",
                "bearing_qualification": "veto_only_not_used_to_shrink_covariance",
            },
            "diagnostics": diagnostics}
