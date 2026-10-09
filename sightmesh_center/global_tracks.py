"""Revisioned Center GlobalTrack materialization.

Only associative identity and representative measured position are produced
here. Multi-sensor correlated state fusion, persistent appearance embedding,
bearing triangulation, late-event optimization and map reasoning remain separate
research modules rather than unverified defaults.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from .association_v1 import (
    AssociationPolicy, GreedySolver, OrToolsSolver, propose_candidates)
from .tracklets import Tracklet, load_committed_tracklets
from .association_v2 import form_consistent_groups
from .fusion_ci import FusionPolicy, estimate_group


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _representative(tracklets: list[Tracklet]) -> dict | None:
    # DO NOT average positions as though estimates from multiple Edge GTSAM
    # filters were independent. Choose a measured representative with provenance.
    candidates = [
        (t.latest, t) for t in tracklets if t.latest is not None]
    if not candidates:
        return None
    item, tracklet = min(
        candidates,
        key=lambda x: (-x[0].quality, sum(x[0].covariance[k]
                 for k in (0, 4, 8)), -x[0].event_ns, x[1].uid))
    return {
        "source_tracklet": tracklet.uid,
        "event_time_ns": item.event_ns,
        "position_map_enu_m": list(item.position),
        "position_covariance_m2": list(item.covariance),
        "velocity_map_enu_mps": list(item.velocity) if item.velocity else None,
        "coordinate_frame_id": item.frame,
        "map_revision": item.map_revision,
        "source": "representative_observation_not_fused",
        "quality": item.quality,
    }


class GlobalTrackRepository:
    def __init__(self, database: str | Path):
        self.database = Path(database).expanduser().resolve()
        if not self.database.exists():
            raise ValueError("Center durable Inbox must be initialized first")
        with closing(self._connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS association_meta(
                  id INTEGER PRIMARY KEY CHECK(id=1),
                  signature TEXT NOT NULL,
                  world_revision INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO association_meta(id, signature, world_revision)
                    VALUES(1, '', 0);
                CREATE TABLE IF NOT EXISTS global_track_members(
                  tracklet_uid TEXT PRIMARY KEY,
                  global_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_global_member_gid
                    ON global_track_members(global_id);
                CREATE TABLE IF NOT EXISTS global_tracks_v1(
                  global_id TEXT PRIMARY KEY,
                  identity_revision INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS identity_revisions_v1(
                  revision INTEGER PRIMARY KEY,
                  world_revision INTEGER NOT NULL,
                  tracklet_uid TEXT NOT NULL,
                  old_global_id TEXT,
                  new_global_id TEXT,
                  reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_identity_revision_world
                    ON identity_revisions_v1(world_revision);
                CREATE TABLE IF NOT EXISTS world_snapshots_v1(
                  world_revision INTEGER PRIMARY KEY,
                  signature TEXT NOT NULL,
                  payload TEXT NOT NULL
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.database), timeout=10, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def latest(self) -> dict:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT payload FROM world_snapshots_v1 "
                "ORDER BY world_revision DESC LIMIT 1").fetchone()
            return json.loads(row[0]) if row else {
                "schema_major": 1,
                "world_revision": 0,
                "global_tracks": [],
                "association_status": "not_evaluated",
            }

    def revisions_after(self, revision: int, limit: int = 1000) -> list[dict]:
        if type(revision) is not int or revision < 0:
            raise ValueError("invalid revision")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid limit")
        with closing(self._connect()) as db:
            rows = db.execute("""
                SELECT revision,world_revision,tracklet_uid,old_global_id,
                       new_global_id,reason
                FROM identity_revisions_v1 WHERE revision>?
                ORDER BY revision LIMIT ?
            """, (revision, limit)).fetchall()
            return [{"revision": row[0], "world_revision": row[1],
                     "tracklet_uid": row[2], "old_global_id": row[3],
                     "new_global_id": row[4], "reason": row[5]} for row in rows]

    def recompute(self, policy: AssociationPolicy,
                  solver_name: str = "greedy",
                  max_events: int = 200_000,
                  fusion_policy: FusionPolicy | None = None) -> dict:
        tracks = load_committed_tracklets(self.database, max_events)
        pairs = propose_candidates(tracks, policy)
        if solver_name == "clique":
            grouped, selected = form_consistent_groups(tracks, pairs)
        elif solver_name in ("greedy", "ortools"):
            # Existing v1 solver semantics remain unchanged for comparison.
            selected = (GreedySolver() if solver_name == "greedy"
                        else OrToolsSolver()).select(tracks, pairs)
            grouped = [{edge.left, edge.right} for edge in selected]
            paired = set().union(*grouped) if grouped else set()
            grouped += [{i} for i in range(len(tracks)) if i not in paired]
        else:
            raise ValueError("solver must be greedy, ortools or clique")

        # This signature covers *all committed history* and the complete
        # explicit alignment policy, not arrival times or wall clock time.
        signature = hashlib.sha256(_canonical({
            "tracklets": [(t.uid, t.event_count, t.start_ns, t.end_ns,
                           [obs.event_id for obs in t.observations])
                          for t in tracks],
            "policy": {
                "sources": {node: vars(cfg) for node, cfg in policy.sources.items()},
                **{k: getattr(policy, k) for k in (
                    "max_pair_dt_s", "maximum_speed_mps", "maximum_sigma_m",
                    "max_mahalanobis_sq", "min_quality", "clock_error_gate_s",
                    "minimum_ray_crossing_deg", "maximum_ray_separation_sigma")}
            },
            "solver": solver_name,
            "fusion_policy": vars(fusion_policy) if fusion_policy is not None else None,
            "fusion_algorithm_revision": 1,
            "class_labels": policy.class_labels,
            "selected": [(tracks[e.left].uid, tracks[e.right].uid,
                          round(e.d2, 8), e.evidence) for e in selected],
            "groups": [sorted(tracks[i].uid for i in g) for g in grouped],
        }).encode()).hexdigest()

        # Sort stable groups and calculate previous membership ownership.
        groups = sorted(
            [sorted((tracks[i] for i in group), key=lambda t: t.uid)
             for group in grouped],
            key=lambda members: tuple(t.uid for t in members))
        # Deterministically evaluate the optional estimator outside the DB
        # write transaction; failure is recorded, never silently substituted
        # with a confident new global position.
        estimates = ([estimate_group(members, policy, fusion_policy)
                      for members in groups]
                     if fusion_policy is not None else None)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                previous_signature, previous_world_revision = db.execute(
                    "SELECT signature,world_revision FROM association_meta WHERE id=1"
                ).fetchone()
                if signature == previous_signature:
                    row = db.execute(
                        "SELECT payload FROM world_snapshots_v1 WHERE world_revision=?",
                        (previous_world_revision,)).fetchone()
                    db.execute("COMMIT")
                    if row:
                        return json.loads(row[0])
                    raise RuntimeError("world signature exists without committed snapshot")

                old_members = dict(db.execute(
                    "SELECT tracklet_uid,global_id FROM global_track_members"))
                old_tracks = {
                    row[0]: (int(row[1]), json.loads(row[2]))
                    for row in db.execute(
                        "SELECT global_id,identity_revision,payload "
                        "FROM global_tracks_v1")
                }
                world_revision = previous_world_revision + 1
                new_members: dict[str, str] = {}
                used_ids: set[str] = set()
                snapshots = []
                # Give first choice to groups with most existing provenance.
                order = sorted(range(len(groups)), key=lambda i: (
                    -max(Counter(old_members.get(t.uid) for t in groups[i]
                                 if old_members.get(t.uid)).values(), default=0),
                    -len(groups[i]), tuple(t.uid for t in groups[i])))
                group_ids: dict[int, str] = {}
                for i in order:
                    members = groups[i]
                    counts = Counter(old_members[t.uid] for t in members
                                     if t.uid in old_members)
                    winner = next(
                        (gid for gid, _ in sorted(counts.items(),
                            key=lambda item: (-item[1], item[0]))
                         if gid not in used_ids), None)
                    if winner is None:
                        # Deterministic provisional ID, never reuse a retired
                        # existing ID from a different historical tracklet.
                        salt = 0
                        while True:
                            value = members[0].uid if salt == 0 else members[0].uid + f"#{salt}"
                            winner = "g-" + hashlib.sha256(value.encode()).hexdigest()[:20]
                            if winner not in used_ids and winner not in old_tracks:
                                break
                            salt += 1
                    group_ids[i] = winner
                    used_ids.add(winner)
                for i, members in enumerate(groups):
                    global_id = group_ids[i]
                    previous = old_tracks.get(global_id)
                    old_set = set(previous[1]["members"]) if previous else set()
                    new_set = {t.uid for t in members}
                    identity_revision = (previous[0] if old_set == new_set
                        else previous[0] + 1 if previous else 1)
                    for member in members:
                        new_members[member.uid] = global_id
                    representative = _representative(members)
                    semantic_classes = {
                        policy.class_labels.get(t.key.node, {}).get(t.class_id)
                        for t in members
                    }
                    semantic_classes.discard(None)
                    class_name = (next(iter(semantic_classes))
                                  if len(semantic_classes) == 1 else "")
                    entry = {
                        "global_id": global_id,
                        "class_name": class_name,
                        "class_id": members[0].class_id,
                        "identity_revision": identity_revision,
                        "status": ("associated_group" if len(members) >= 3 else
                                   "associated_pair" if len(members) == 2 else
                                   "provisional" if representative else "unlocalized"),
                        "members": sorted(new_set),
                        "start_event_time_ns": min(t.start_ns for t in members),
                        "end_event_time_ns": max(t.end_ns for t in members),
                        "source_count": len({t.key.node for t in members}),
                        "representative": representative,
                        # This field describes the existing published 3D
                        # representative, not the experimental CI sidecar.
                        "fusion": "NOT_FUSED",
                        "assignment_method": solver_name,
                    }
                    if estimates is not None:
                        evaluation = estimates[i]
                        entry["fusion_estimate"] = evaluation.get("estimate")
                        entry["fusion_estimate_status"] = (
                            "experimental_ci" if evaluation["accepted"]
                            else "rejected")
                        entry["fusion_rejection_reason"] = evaluation["reason"]
                        entry["fusion_diagnostics"] = evaluation["diagnostics"]
                    snapshots.append(entry)

                # Revisions are append-only and must be stored in the SAME
                # transaction as updated membership/world snapshot.
                revision = db.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM identity_revisions_v1"
                ).fetchone()[0]
                for uid in sorted(set(old_members) | set(new_members)):
                    old_id, new_id = old_members.get(uid), new_members.get(uid)
                    if old_id == new_id:
                        continue
                    revision += 1
                    reason = ("new" if old_id is None else
                              "retired" if new_id is None else "identity_revised")
                    db.execute("""
                        INSERT INTO identity_revisions_v1
                           (revision,world_revision,tracklet_uid,
                            old_global_id,new_global_id,reason)
                        VALUES(?,?,?,?,?,?)
                    """, (revision, world_revision, uid, old_id, new_id, reason))

                db.execute("DELETE FROM global_track_members")
                db.executemany(
                    "INSERT INTO global_track_members(tracklet_uid,global_id) VALUES(?,?)",
                    sorted(new_members.items()))
                db.execute("DELETE FROM global_tracks_v1")
                db.executemany("""
                    INSERT INTO global_tracks_v1
                        (global_id,identity_revision,status,payload)
                    VALUES(?,?,?,?)
                """, [(item["global_id"], item["identity_revision"],
                        item["status"], _canonical(item)) for item in snapshots])
                world = {
                    "schema_major": 1,
                    "world_revision": world_revision,
                    "association_status": "conservative_baseline_not_joint_fusion",
                    "fusion_mode": ("experimental_ci_sidecar_not_published"
                                    if fusion_policy is not None else "off"),
                    "experimental_fusion_accepted": (
                        sum(1 for item in estimates if item["accepted"])
                        if estimates is not None else 0),
                    "clock_policy": "explicitly_verified_sources_only",
                    "alignment_policy": {
                        node: {"alignment_id": v.alignment_id,
                               "coordinate_frame_id": v.coordinate_frame_id,
                               "map_revision": v.map_revision,
                               "clock_domain": v.domain,
                               "clock_uncertainty_ns": v.clock_uncertainty_ns}
                        for node, v in policy.sources.items()
                    },
                    "solver": solver_name,
                    "candidate_count": len(pairs),
                    "selected_pair_count": len(selected),
                    "associated_group_count": sum(1 for group in grouped if len(group) >= 3),
                    "association_evidence": "pairwise_clique_no_joint_state_fusion"
                        if solver_name == "clique" else "one_to_one_no_joint_state_fusion",
                    "global_tracks": sorted(snapshots,
                                            key=lambda x: x["global_id"]),
                    "last_identity_revision": revision,
                }
                db.execute("""
                    INSERT INTO world_snapshots_v1(world_revision,signature,payload)
                    VALUES(?,?,?)
                """, (world_revision, signature, _canonical(world)))
                db.execute("""
                    UPDATE association_meta SET signature=?,world_revision=? WHERE id=1
                """, (signature, world_revision))
                db.execute("COMMIT")
                return world
            except Exception:
                db.execute("ROLLBACK")
                raise
