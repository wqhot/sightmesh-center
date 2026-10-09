"""Independent, conservative ray geometry evidence for cross-node association.

Reads the exact fields emitted by Edge JsonProtocolCodec::EncodeEventObject:
spatial.bearing.camera_position_world_m, direction_world,
has_tangent_covariance, tangent_covariance_rad2. Missing/invalid bearing
never creates a position estimate and cannot make an impossible pair valid.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Optional


@dataclass(frozen=True)
class Ray:
    origin: tuple[float, float, float]
    direction: tuple[float, float, float]
    angular_sigma_rad: float


@dataclass(frozen=True)
class RayEvidence:
    separation_m: float
    crossing_angle_deg: float
    normalized_separation: float
    forward: bool
    informative: bool


def _vec3(data: Any) -> Optional[tuple[float, float, float]]:
    if type(data) not in (tuple, list) or len(data) != 3:
        return None
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in data):
        return None
    return tuple(float(v) for v in data)


def read_ray(value: Any) -> Optional[Ray]:
    if type(value) is not dict or value.get("valid") is not True:
        return None
    origin = _vec3(value.get("camera_position_world_m"))
    direction = _vec3(value.get("direction_world"))
    if origin is None or direction is None:
        return None
    norm = math.sqrt(sum(x * x for x in direction))
    if not 0.98 <= norm <= 1.02:  # input must already be calibrated unit ray
        return None
    direction = tuple(x / norm for x in direction)
    if value.get("has_tangent_covariance") is not True:
        return None
    c = value.get("tangent_covariance_rad2")
    if type(c) not in (list, tuple) or len(c) != 4 or any(
        type(x) not in (int, float) or not math.isfinite(x) for x in c
    ):
        return None
    a, b, other, d = (float(x) for x in c)
    if (a <= 0 or d <= 0 or a > 0.25 or d > 0.25 or
        abs(b - other) > 1e-7 or a * d - b * b <= 1e-12):
        return None
    # Bound the largest eigenvalue, not the mean variance.
    eigen_max = (a + d + math.sqrt((a - d) ** 2 + 4 * b * b)) / 2
    return Ray(origin, direction, math.sqrt(eigen_max))


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _norm(a):
    return math.sqrt(_dot(a, a))


def assess_rays(
    first: Ray, second: Ray,
    position_sigma_m: float,
    clock_position_tolerance_m: float,
    minimum_crossing_angle_deg: float = 3.0,
    max_normalized_gap: float = 4.0,
) -> RayEvidence:
    """Closest *forward-ray* separation; no triangulated position emitted.

    Nearly parallel rays are weak/ambiguous evidence: informative=False,
    and callers must NOT treat their small separation as association support.
    """
    p, q = first.origin, second.origin
    u, v = first.direction, second.direction
    w = tuple(p[i] - q[i] for i in range(3))
    uv = max(-1.0, min(1.0, _dot(u, v)))
    angle = math.degrees(math.acos(abs(uv)))
    det = 1.0 - uv * uv
    if det < 1e-8:
        # Closest points for almost parallel rays are ill-conditioned.
        return RayEvidence(float("inf"), angle, float("inf"), False, False)
    uw = _dot(u, w)
    vw = _dot(v, w)
    along_first = (uv * vw - uw) / det
    along_second = (vw - uv * uw) / det
    forward = along_first > 0 and along_second > 0
    closest = tuple(w[i] + along_first * u[i] -
                    along_second * v[i] for i in range(3))
    separation = _norm(closest)
    # Angular uncertainty becomes transverse metric error proportional to
    # each distance. Position/clock uncertainty contributes in meters.
    variance = (max(position_sigma_m, 0.0) ** 2 +
                max(clock_position_tolerance_m, 0.0) ** 2 +
                (along_first * first.angular_sigma_rad) ** 2 +
                (along_second * second.angular_sigma_rad) ** 2)
    normalized = separation / max(math.sqrt(variance), 1e-3)
    informative = angle >= minimum_crossing_angle_deg
    return RayEvidence(separation, angle, normalized, forward, informative)


def bearing_pair_consistent(
    ray_a: Ray, ray_b: Ray, sigma_m: float, clock_tolerance_m: float,
    min_crossing_deg: float, max_normalized_gap: float,
) -> tuple[bool, str, Optional[RayEvidence]]:
    evidence = assess_rays(
        ray_a, ray_b, sigma_m, clock_tolerance_m,
        min_crossing_deg, max_normalized_gap)
    if not evidence.informative:
        # Insufficient parallax => never positive evidence or hard rejection.
        return True, "weak-parallax-not-used", evidence
    if not evidence.forward or evidence.normalized_separation > max_normalized_gap:
        return False, "ray-geometry-contradiction", evidence
    return True, "ray-consistent", evidence
