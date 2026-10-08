"""Event-time Tracklet reconstruction from *committed* Edge TrackEvents.

This module is deliberately free of NNG/HTTP, Protobuf, ReID, OR-Tools and
numerical packages. A track key includes (node,camera,session,local_id); event
time is never replaced with Center receive time.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Optional


@dataclass(frozen=True, order=True)
class TrackKey:
    node: str
    camera: str
    session: int
    local_id: int

    def encoded(self) -> str:
        # JSON encoding is unambiguous even if names contain punctuation.
        return json.dumps([self.node, self.camera, self.session, self.local_id],
                          ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Observation:
    event_id: str
    seq: int
    event_ns: int
    frame_id: int
    class_id: int
    position: Optional[tuple[float, float, float]]
    covariance: Optional[tuple[float, ...]]
    velocity: Optional[tuple[float, float, float]]
    frame: str
    map_revision: str
    quality: float
    bearing: Optional[dict]


@dataclass
class Tracklet:
    key: TrackKey
    segment_start_seq: int
    start_ns: int
    end_ns: int
    closed: bool = False
    observations: list[Observation] = field(default_factory=list)
    event_count: int = 0

    @property
    def uid(self) -> str:
        return json.dumps([self.key.node, self.key.camera,
                           self.key.session, self.key.local_id,
                           self.segment_start_seq],
                          ensure_ascii=False, separators=(",", ":"))

    @property
    def latest(self) -> Optional[Observation]:
        # Do not use a seconds-old valid world estimate when subsequent Edge
        # updates have become unobservable/invalid. This is a safety bound
        # independent of any between-node packet timing threshold.
        for item in reversed(self.observations):
            if self.end_ns - item.event_ns > 250_000_000:
                break
            if item.position is not None and item.covariance is not None:
                return item
        return None

    @property
    def class_id(self) -> Optional[int]:
        for item in reversed(self.observations):
            if item.class_id >= 0:
                return item.class_id
        return None


def _finite_vector(values: Any, count: int) -> Optional[tuple[float, ...]]:
    if (type(values) not in (list, tuple) or len(values) != count
            or any(type(v) not in (int, float) or
                   not math.isfinite(v) for v in values)):
        return None
    return tuple(float(x) for x in values)


def _covariance3(values: Any) -> Optional[tuple[float, ...]]:
    """Validate an actual symmetric positive-definite 3x3 covariance.

    Invalid, zero and indefinite covariance must not be treated as a measured
    uncertainty in association OR published as a localized GlobalTrack.
    """
    data = _finite_vector(values, 9)
    if data is None:
        return None
    L = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(i):
            if abs(data[i * 3 + j] - data[j * 3 + i]) > 1e-5:
                return None
        for j in range(i + 1):
            v = data[i * 3 + j] - sum(L[i][k] * L[j][k]
                                      for k in range(j))
            if i == j:
                if v <= 1e-10 or not math.isfinite(v):
                    return None
                L[i][j] = math.sqrt(v)
            else:
                L[i][j] = v / L[j][j]
    return data


def _observe(event: dict) -> Observation:
    spatial = event.get("spatial")
    spatial = spatial if type(spatial) is dict else {}
    world = spatial.get("world", {})
    world = world if type(world) is dict else {}
    loc_quality = spatial.get("localization_quality", {})
    loc_quality = loc_quality if type(loc_quality) is dict else {}
    map_info = loc_quality.get("map", {})
    map_info = map_info if type(map_info) is dict else {}

    position = _finite_vector([world.get("x_m"), world.get("y_m"),
                               world.get("z_m")], 3) if world.get("valid") is True else None
    covariance = _covariance3(world.get("position_covariance_m2"))
    velocity = (_finite_vector([world.get("vx_mps"), world.get("vy_mps"),
                                world.get("vz_mps")], 3)
                if world.get("velocity_valid") is True else None)
    frame = world.get("coordinate_frame_id")
    frame = frame if isinstance(frame, str) else ""
    revision = map_info.get("revision") if map_info.get("valid") is True else ""
    revision = revision if isinstance(revision, str) else ""
    quality = world.get("quality", 0)
    quality = float(quality) if type(quality) in (float, int) and math.isfinite(quality) else 0.0
    # Unknown covariance, coordinate frame or map revision => not usable for
    # automated multi-node association, but keep original bearing and events.
    if not frame or not revision:
        position = None
    if position is None:
        covariance = None
    bearing = spatial.get("bearing")
    return Observation(
        event_id=str(event["event_id"]),
        seq=int(event["seq"]), event_ns=int(event["event_time_ns"]),
        frame_id=int(event["frame_id"]),
        class_id=int(event["class_id"]),
        position=position, covariance=covariance, velocity=velocity,
        frame=frame, map_revision=revision, quality=quality,
        bearing=bearing if isinstance(bearing, dict) else None)


def load_committed_tracklets(database: str | Path,
                             max_events: int = 200_000) -> list[Tracklet]:
    """Read only contiguous, blob-complete records from the durable Inbox.

    Rebuilds deterministically from full event history; a late event therefore
    changes the next association proposal rather than being discarded by an
    arrival-order cursor. V1 deliberately bounds the history size.
    """
    if max_events <= 0:
        raise ValueError("max_events must be positive")
    connection = sqlite3.connect(str(database), timeout=10)
    try:
        rows = connection.execute("""
            SELECT e.payload FROM events AS e
            JOIN sources AS s ON s.node=e.node AND e.seq<=s.contiguous
            ORDER BY e.node, e.seq LIMIT ?
        """, (max_events + 1,)).fetchall()
    finally:
        connection.close()
    if len(rows) > max_events:
        raise ValueError("committed event history exceeds configured bound; "
                         "requires checkpoint/retention design")
    by_key: dict[TrackKey, list[dict]] = defaultdict(list)
    for (raw,) in rows:
        event = json.loads(raw)
        key = TrackKey(str(event["node_id"]), str(event["camera_id"]),
                       int(event["session_id"]), int(event["local_id"]))
        by_key[key].append(event)

    results: list[Tracklet] = []
    for key, events in sorted(by_key.items()):
        # Tracklet lifetime is reconstructed in SOURCE EVENT TIME, not HTTP
        # delivery, WAL commit, frame arrival, or database primary key order.
        events.sort(key=lambda e: (int(e["event_time_ns"]), int(e["seq"])))
        active: Optional[Tracklet] = None
        for event in events:
            kind = event["type"]
            if active is None or (kind == "start" and active.event_count > 0):
                if active is not None:
                    results.append(active)
                active = Tracklet(key, int(event["seq"]),
                                  int(event["event_time_ns"]),
                                  int(event["event_time_ns"]))
            timestamp = int(event["event_time_ns"])
            active.start_ns = min(active.start_ns, timestamp)
            active.end_ns = max(active.end_ns, timestamp)
            active.event_count += 1
            active.observations.append(_observe(event))
            if kind == "end":
                active.closed = True
                results.append(active)
                active = None
        if active is not None:
            results.append(active)
    return sorted(results, key=lambda t: (t.start_ns, t.uid))
