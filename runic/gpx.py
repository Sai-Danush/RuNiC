"""Parse a GPX route into a terrain profile (distance + grade segments)."""

from __future__ import annotations

import math
from pathlib import Path

import gpxpy

from .models import RouteProfile, Segment, Terrain

# Grade thresholds (%) -> terrain class. Tunable.
_GRADE_BANDS: list[tuple[float, Terrain]] = [
    (8.0, Terrain.STEEP_UP),
    (3.0, Terrain.UP),
    (-3.0, Terrain.FLAT),
    (-8.0, Terrain.DOWN),
]
_FLOOR_TERRAIN = Terrain.STEEP_DOWN

_EARTH_RADIUS_M = 6_371_000.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _classify(grade_pct: float) -> Terrain:
    for threshold, terrain in _GRADE_BANDS:
        if grade_pct >= threshold:
            return terrain
    return _FLOOR_TERRAIN


def _smooth(values: list[float], window: int) -> list[float]:
    """Simple centered rolling-mean smoother (odd window). Tames GPS noise."""
    if window <= 1 or len(values) < window:
        return list(values)
    half = window // 2
    out: list[float] = []
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        window_vals = values[lo:hi]
        out.append(sum(window_vals) / len(window_vals))
    return out


def parse_route(
    gpx_path: str | Path,
    *,
    segment_length_m: float = 200.0,
    smooth_window: int = 5,
) -> RouteProfile:
    """Parse a GPX file into a :class:`RouteProfile`.

    Points are reduced to cumulative distance + (smoothed) elevation, then bucketed
    into fixed-length segments. Each segment gets an average grade and terrain class.

    Args:
        gpx_path: path to the .gpx file.
        segment_length_m: target length of each terrain segment.
        smooth_window: rolling-mean window (points) applied to elevation.
    """
    path = Path(gpx_path)
    if not path.exists():
        raise FileNotFoundError(f"GPX file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        gpx = gpxpy.parse(fh)

    # Flatten all track points across tracks/segments in order.
    lats: list[float] = []
    lons: list[float] = []
    eles: list[float] = []
    for track in gpx.tracks:
        for seg in track.segments:
            for pt in seg.points:
                if pt.latitude is None or pt.longitude is None:
                    continue
                lats.append(pt.latitude)
                lons.append(pt.longitude)
                eles.append(pt.elevation if pt.elevation is not None else 0.0)

    if len(lats) < 2:
        raise ValueError("GPX route has fewer than 2 usable track points.")

    eles = _smooth(eles, smooth_window)

    # Cumulative distance per point.
    cum: list[float] = [0.0]
    for i in range(1, len(lats)):
        cum.append(cum[-1] + _haversine_m(lats[i - 1], lons[i - 1], lats[i], lons[i]))
    total_distance = cum[-1]

    # Totals (raw point-to-point on smoothed elevation).
    ascent = sum(max(0.0, eles[i] - eles[i - 1]) for i in range(1, len(eles)))
    descent = sum(max(0.0, eles[i - 1] - eles[i]) for i in range(1, len(eles)))

    segments = _build_segments(cum, eles, segment_length_m)
    return RouteProfile(
        segments=segments,
        total_distance_m=total_distance,
        total_ascent_m=ascent,
        total_descent_m=descent,
    )


def _elevation_at(cum: list[float], eles: list[float], target_m: float) -> float:
    """Linear-interpolate elevation at a cumulative distance."""
    if target_m <= cum[0]:
        return eles[0]
    if target_m >= cum[-1]:
        return eles[-1]
    # Binary-ish linear scan is fine for typical route sizes.
    for i in range(1, len(cum)):
        if cum[i] >= target_m:
            span = cum[i] - cum[i - 1]
            if span <= 0:
                return eles[i]
            frac = (target_m - cum[i - 1]) / span
            return eles[i - 1] + frac * (eles[i] - eles[i - 1])
    return eles[-1]


def _build_segments(
    cum: list[float], eles: list[float], segment_length_m: float
) -> list[Segment]:
    total = cum[-1]
    if total <= 0:
        raise ValueError("GPX route has zero length.")

    segments: list[Segment] = []
    start = 0.0
    while start < total - 1e-6:
        end = min(start + segment_length_m, total)
        ele_start = _elevation_at(cum, eles, start)
        ele_end = _elevation_at(cum, eles, end)
        run = end - start
        grade = ((ele_end - ele_start) / run * 100.0) if run > 0 else 0.0
        segments.append(
            Segment(
                start_m=start,
                end_m=end,
                grade_pct=grade,
                terrain=_classify(grade),
            )
        )
        start = end
    return segments
