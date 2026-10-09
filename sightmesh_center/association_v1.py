"""Conservative, transparent Center V1 cross-node spatial association.

No learned appearance baseline and no invented covariance/time/map alignment.
AssociationSolver selects disjoint *pairs*; an association pair is NOT yet
a multi-camera joint 3D estimate. Optional OR-Tools is solver-only.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Protocol

from .tracklets import Tracklet, Observation
from .bearing_geometry import read_ray, bearing_pair_consistent


@dataclass(frozen=True)
class SourceAlignment:
    domain: str
    offset_ns: int
    clock_uncertainty_ns: int
    alignment_id: str
    coordinate_frame_id: str
    map_revision: str


@dataclass(frozen=True)
class AssociationPolicy:
    sources: dict[str, SourceAlignment]
    class_labels: dict[str, dict[int, str]]
    max_pair_dt_s: float = 0.25
    maximum_speed_mps: float = 30.0
    maximum_sigma_m: float = 15.0
    max_mahalanobis_sq: float = 11.344866730144373  # chi2_3 99%
    min_quality: float = 0.0
    clock_error_gate_s: float = 0.05
    minimum_ray_crossing_deg: float = 3.0
    maximum_ray_separation_sigma: float = 4.0

    @staticmethod
    def from_dict(configuration: dict) -> "AssociationPolicy":
        if not isinstance(configuration, dict):
            raise ValueError("association policy must be an object")
        entries = configuration.get("sources")
        if type(entries) is not dict:
            raise ValueError("association.sources is required; no implicit clock/frame trust")
        sources = {}
        for node, item in entries.items():
            if not isinstance(node, str) or not node or type(item) is not dict:
                raise ValueError("invalid source alignment entry")
            if item.get("verified") is not True:
                continue  # Unverified sources remain unassociated singletons.
            domain = item.get("domain")
            if domain not in ("shared_utc", "shared_sim"):
                raise ValueError(f"source {node}: only shared_utc/shared_sim may be associated")
            origin = item.get("alignment_id")
            frame = item.get("coordinate_frame_id")
            revision = item.get("map_revision")
            if not all(isinstance(x, str) and x for x in (origin, frame, revision)):
                raise ValueError(f"source {node}: verified frame/map/alignment required")
            offset = item.get("offset_ns", 0)
            uncertainty = item.get("clock_uncertainty_ns")
            if type(offset) is not int or abs(offset) > 60 * 1_000_000_000:
                raise ValueError(f"source {node}: invalid clock offset")
            if type(uncertainty) is not int or not 0 <= uncertainty <= 1_000_000_000:
                raise ValueError(f"source {node}: clock uncertainty required [0,1s]")
            sources[node] = SourceAlignment(
                domain, offset, uncertainty, origin, frame, revision)

        class_labels = {}
        raw_labels = configuration.get("class_labels", {})
        if type(raw_labels) is not dict:
            raise ValueError("association.class_labels must be node -> {class_id: label}")
        for node, labels in raw_labels.items():
            if type(labels) is not dict:
                raise ValueError("class label map must be an object")
            class_labels[node] = {}
            for key, name in labels.items():
                if (type(key) is not str or not key.isdecimal() or
                    type(name) is not str or not name.strip() or
                    len(name) > 128):
                    raise ValueError("invalid model-specific class mapping")
                class_labels[node][int(key)] = name.strip().lower()

        params = {}
        limits = {
            "max_pair_dt_s": (0, 2),
            "maximum_speed_mps": (0.1, 200),
            "maximum_sigma_m": (0.001, 100),
            "max_mahalanobis_sq": (0.1, 100),
            "min_quality": (0, 1),
            "clock_error_gate_s": (0, 1),
            "minimum_ray_crossing_deg": (0.1, 40),
            "maximum_ray_separation_sigma": (0.1, 10),
        }
        for name, (low, high) in limits.items():
            value = configuration.get(name, getattr(AssociationPolicy, name))
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"invalid association.{name}")
            params[name] = float(value)
        return AssociationPolicy(sources=sources, class_labels=class_labels, **params)


@dataclass(frozen=True)
class PairCandidate:
    left: int
    right: int
    cost: float
    d2: float
    elapsed_s: float
    evidence: str = "aligned_WGS84_map_ENU_mahalanobis3"


class CandidateSolver(Protocol):
    def select(self, tracklets: list[Tracklet],
               candidates: list[PairCandidate]) -> list[PairCandidate]: ...


class GreedySolver:
    """Deterministic one-to-one baseline; *not* claimed globally optimal."""
    def select(self, tracklets: list[Tracklet],
               candidates: list[PairCandidate]) -> list[PairCandidate]:
        seen = set()
        result = []
        for edge in sorted(candidates,
                           key=lambda x: (x.cost, tracklets[x.left].uid,
                                          tracklets[x.right].uid)):
            if edge.left not in seen and edge.right not in seen:
                result.append(edge)
                seen.add(edge.left)
                seen.add(edge.right)
        return result


class OrToolsSolver:
    """Optional exact max-weight disjoint-pair matching, not a cost model.

    Intentionally imports OR-Tools lazily; D2000 and normal map serving need
    neither Python OR-Tools nor an incompatible x86 wheel.
    """
    def select(self, tracklets: list[Tracklet],
               candidates: list[PairCandidate]) -> list[PairCandidate]:
        if not candidates:
            return []
        try:
            from ortools.sat.python import cp_model
        except ImportError as exc:
            raise RuntimeError("OR-Tools is optional; install it only on supported x86") from exc
        model = cp_model.CpModel()
        ordered = sorted(candidates,
                         key=lambda c: (tracklets[c.left].uid,
                                        tracklets[c.right].uid))
        vars_ = [model.NewBoolVar(f"link_{i}") for i in range(len(ordered))]
        for idx in range(len(tracklets)):
            incident = [vars_[i] for i, pair in enumerate(ordered)
                        if idx in (pair.left, pair.right)]
            if incident:
                model.Add(sum(incident) <= 1)
        # Lexicographic cardinality then physical cost. The cardinality
        # reward exceeds ALL possible accumulated edge-cost differences.
        if len(ordered) > 10_000:
            raise ValueError("OR-Tools candidate cap exceeded")
        cardinality_reward = (len(tracklets) // 2 + 1) * 100001
        model.Maximize(sum((cardinality_reward -
                            min(100000, int(edge.cost * 1000))) *
                           vars_[i] for i, edge in enumerate(ordered)))
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        solver.parameters.max_time_in_seconds = 3.0
        solver.parameters.random_seed = 0
        status = solver.Solve(model)
        if status != cp_model.OPTIMAL:
            raise RuntimeError("OR-Tools matching was not proven optimal; "
                               "do not silently label a partial solution optimal")
        return [edge for i, edge in enumerate(ordered) if solver.Value(vars_[i])]


def _spd_quadratic(diff: tuple[float, float, float],
                   a: tuple[float, ...], b: tuple[float, ...],
                   extra_variance: float, maximum_sigma: float) -> float | None:
    # Reject non-symmetric, implausible or not-PD covariances rather than
    # substituting the identity. Full 3x3 Cholesky, not diagonal-only gating.
    for cov in (a, b):
        if len(cov) != 9 or any(not math.isfinite(x) for x in cov):
            return None
        if any(cov[i * 3 + i] <= 0 or
               cov[i * 3 + i] > maximum_sigma ** 2 for i in range(3)):
            return None
        for i in range(3):
            for j in range(i):
                if abs(cov[i * 3 + j] - cov[j * 3 + i]) > 1e-5:
                    return None
    mat = [[a[i * 3 + j] + b[i * 3 + j] +
            (extra_variance if i == j else 0.0) for j in range(3)]
           for i in range(3)]
    L = [[0.0] * 3 for _ in range(3)]
    try:
        for i in range(3):
            for j in range(i + 1):
                accum = mat[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
                if i == j:
                    if accum <= 1e-10 or not math.isfinite(accum):
                        return None
                    L[i][j] = math.sqrt(accum)
                else:
                    L[i][j] = accum / L[j][j]
        y = [0.0] * 3
        for i in range(3):
            y[i] = (diff[i] - sum(L[i][j] * y[j] for j in range(i))) / L[i][i]
        return sum(x * x for x in y)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def _candidate(a: Tracklet, b: Tracklet,
               policy: AssociationPolicy) -> PairCandidate | None:
    if a.key.node == b.key.node:
        return None   # V1 links only separate physical nodes.
    if a.class_id is None or b.class_id is None:
        return None
    # Numeric class IDs are MODEL-LOCAL. Require explicit common semantic
    # class labels; custom tank class 0 must not match COCO person class 0.
    label_a = policy.class_labels.get(a.key.node, {}).get(a.class_id)
    label_b = policy.class_labels.get(b.key.node, {}).get(b.class_id)
    if not label_a or label_a != label_b:
        return None
    sa = policy.sources.get(a.key.node)
    sb = policy.sources.get(b.key.node)
    if sa is None or sb is None:
        return None
    if (sa.domain != sb.domain or sa.alignment_id != sb.alignment_id or
        sa.coordinate_frame_id != sb.coordinate_frame_id or
        sa.map_revision != sb.map_revision):
        return None

    oa, ob = a.latest, b.latest
    if oa is None or ob is None:
        return None
    if (oa.frame != sa.coordinate_frame_id or
        ob.frame != sb.coordinate_frame_id or
        oa.map_revision != sa.map_revision or
        ob.map_revision != sb.map_revision or
        oa.quality < policy.min_quality or ob.quality < policy.min_quality):
        return None
    ta = oa.event_ns + sa.offset_ns
    tb = ob.event_ns + sb.offset_ns
    delta = (tb - ta) / 1e9
    uncertainty_s = (sa.clock_uncertainty_ns + sb.clock_uncertainty_ns) / 1e9
    if uncertainty_s > policy.clock_error_gate_s or abs(delta) > policy.max_pair_dt_s:
        return None

    # Only short-horizon extrapolation using valid velocity. No velocity
    # covariance is provided by the legacy event, so acceleration/clock
    # inflation is explicit and long-horizon matching is disallowed.
    p = list(oa.position)
    q = list(ob.position)
    if delta > 0 and oa.velocity is not None:
        p = [p[i] + oa.velocity[i] * delta for i in range(3)]
    elif delta < 0 and ob.velocity is not None:
        q = [q[i] - ob.velocity[i] * delta for i in range(3)]
    diff = tuple(p[i] - q[i] for i in range(3))
    dt = abs(delta) + uncertainty_s
    variance = (0.5 * policy.maximum_speed_mps * dt) ** 2
    d2 = _spd_quadratic(diff, oa.covariance, ob.covariance,
                        variance, policy.maximum_sigma_m)
    if d2 is None or d2 > policy.max_mahalanobis_sq:
        return None
    # Complement covariance gate with a conservative physical envelope.
    sigma = math.sqrt(max(oa.covariance[0], oa.covariance[4],
                          oa.covariance[8], ob.covariance[0],
                          ob.covariance[4], ob.covariance[8]))
    distance = math.sqrt(sum(x * x for x in diff))
    if distance > policy.maximum_speed_mps * dt + 3.5 * sigma:
        return None

    # Bearing observations are independent geometric evidence when both
    # sources provide valid tangent covariance. The two rays must already
    # belong to the same VERIFIED frame/clock/map; near-parallel rays are
    # uninformative, not an invitation to invent depth by triangulation.
    # Edge bearing and world localization may refer to different samples
    # inside a TrackEvent. Compare in the source clock domain before using
    # the two rays as evidence; unknown time relation => no bearing veto.
    def aligned_ray(obs):
        if type(obs.bearing) is not dict:
            return None
        when = obs.bearing.get("timestamp_ns")
        if type(when) is not int or when <= 0 or abs(when - obs.event_ns) > 250_000_000:
            return None
        return read_ray(obs.bearing)

    ray_a, ray_b = aligned_ray(oa), aligned_ray(ob)
    bearing_evidence = "position-only"
    if ray_a is not None and ray_b is not None:
        consistent, bearing_evidence, _ = bearing_pair_consistent(
            ray_a, ray_b, sigma, policy.maximum_speed_mps * dt,
            policy.minimum_ray_crossing_deg,
            policy.maximum_ray_separation_sigma)
        if not consistent:
            return None

    # No covariance fusion, triangulation, appearance score or map semantics
    # are fabricated. Ray evidence can reject a conflicting pair but does
    # not lower cost as if it were independent Gaussian confidence.
    cost = d2 + 0.25 * abs(delta) / max(policy.max_pair_dt_s, 1e-6)
    return PairCandidate(-1, -1, cost, d2, abs(delta), bearing_evidence)


def propose_candidates(tracklets: list[Tracklet],
                       policy: AssociationPolicy,
                       max_candidates: int = 10_000) -> list[PairCandidate]:
    if len(tracklets) > 5000:
        raise ValueError("too many tracklets for bounded V1 pairwise pass")
    result = []
    for i, left in enumerate(tracklets):
        for j in range(i + 1, len(tracklets)):
            edge = _candidate(left, tracklets[j], policy)
            if edge is not None:
                result.append(PairCandidate(i, j, edge.cost, edge.d2,
                                            edge.elapsed_s, edge.evidence))
                if len(result) > max_candidates:
                    raise ValueError("candidate cap exceeded; use a spatial index before scaling")
    return result
