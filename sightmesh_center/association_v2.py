"""V2 bounded all-pairs-consistent multi-node association.

An edge alone cannot transitively merge three tracks. Any combined group must
be a complete candidate clique, one tracklet per physical node, with every
pair having passed the SAME class/clock/map/covariance/bearing gates.

This is a conservative deterministic baseline, *not* correlation-aware global
graph optimization / OR-Tools multicut and not a joint state estimator.
"""
from __future__ import annotations

from .association_v1 import PairCandidate
from .tracklets import Tracklet


def form_consistent_groups(
    tracks: list[Tracklet],
    candidates: list[PairCandidate],
    max_group_size: int = 4,
) -> tuple[list[set[int]], list[PairCandidate]]:
    if not 2 <= max_group_size <= 8:
        raise ValueError("max_group_size must be 2..8")
    n = len(tracks)
    edges: dict[tuple[int, int], PairCandidate] = {}
    for edge in candidates:
        x, y = sorted((edge.left, edge.right))
        if not 0 <= x < y < n:
            raise ValueError("invalid candidate index")
        key = (x, y)
        if key in edges:
            raise ValueError("duplicate candidate pair")
        edges[key] = edge

    # Bounded clustering: only merge two existing groups if ALL cross-pairs
    # exist and every node is unique. A-B, B-C without A-C is never a group.
    parent = list(range(n))
    groups: dict[int, set[int]] = {i: {i} for i in range(n)}
    def root(i):
        while i != parent[i]:
            i = parent[i]
        return i

    ordered = sorted(candidates, key=lambda e: (
        e.cost, tracks[e.left].uid, tracks[e.right].uid))
    for edge in ordered:
        ra, rb = root(edge.left), root(edge.right)
        if ra == rb:
            continue
        joined = groups[ra] | groups[rb]
        if len(joined) > max_group_size:
            continue
        if len({tracks[i].key.node for i in joined}) != len(joined):
            continue
        pairs = [tuple(sorted((i, j))) for i in sorted(joined)
                 for j in sorted(joined) if i < j]
        if not all(p in edges for p in pairs):
            continue
        # Merge into stable lowest root; deterministic on duplicate costs.
        target, consumed = min(ra, rb), max(ra, rb)
        parent[consumed] = target
        groups[target] = joined
        del groups[consumed]

    grouped = sorted(groups.values(), key=lambda group: (
        min(tracks[i].uid for i in group), len(group)))
    used_edges = [
        edges[(i, j)] for group in grouped for i in sorted(group)
        for j in sorted(group) if i < j
    ]
    return grouped, used_edges
