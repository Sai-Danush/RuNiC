"""Pace & cadence model.

Two inputs, merged:
  * Manual PBs (e.g. "5k=22:30") -> predicted flat-equivalent speed via Riegel.
  * Past-run GPX -> measured average speed and (if recorded) cadence.

Per-segment time uses a Minetti-based grade cost so climbs take longer than
descents, which is what places songs correctly on the TIME axis.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import gpxpy

from .models import Segment

# Named race distances in metres.
_NAMED_DISTANCES = {
    "5k": 5_000.0,
    "10k": 10_000.0,
    "15k": 15_000.0,
    "half": 21_097.5,
    "21k": 21_097.5,
    "marathon": 42_195.0,
    "full": 42_195.0,
    "42k": 42_195.0,
}

_RIEGEL_EXPONENT = 1.06
_FLAT_COST = 3.6           # Minetti flat cost of running (J/kg/m)
_DEFAULT_CADENCE_SPM = 170.0
_COST_FACTOR_BOUNDS = (0.55, 2.6)  # clamp grade-adjustment so it stays sane


@dataclass(frozen=True)
class PaceModel:
    """Flat-equivalent running speed plus a target cadence."""

    base_speed_mps: float
    cadence_spm: float


def parse_duration(text: str) -> float:
    """Parse 'mm:ss' or 'h:mm:ss' (or plain seconds) into seconds."""
    text = text.strip()
    if ":" not in text:
        return float(text)
    parts = [float(p) for p in text.split(":")]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def parse_distance(token: str) -> float:
    """Parse a distance token into metres ('5k', 'half', '3200', '10.5k')."""
    token = token.strip().lower()
    if token in _NAMED_DISTANCES:
        return _NAMED_DISTANCES[token]
    if token.endswith("k"):
        return float(token[:-1]) * 1_000.0
    if token.endswith("km"):
        return float(token[:-2]) * 1_000.0
    if token.endswith("m"):
        return float(token[:-1])
    return float(token)  # bare number = metres


def parse_pb(spec: str) -> tuple[float, float]:
    """Parse a PB spec like '5k=22:30' into (distance_m, time_s)."""
    if "=" not in spec:
        raise ValueError(f"PB must look like '5k=22:30', got: {spec!r}")
    dist_tok, time_tok = spec.split("=", 1)
    return parse_distance(dist_tok), parse_duration(time_tok)


def riegel_predict(known_dist_m: float, known_time_s: float, target_dist_m: float) -> float:
    """Riegel: T2 = T1 * (D2/D1)^1.06. Returns predicted time (s) for target."""
    return known_time_s * (target_dist_m / known_dist_m) ** _RIEGEL_EXPONENT


def minetti_cost_factor(grade_pct: float) -> float:
    """Relative energy cost of running at a grade vs flat (Minetti 2002).

    Returns a multiplier on time-per-metre: 1.0 on the flat, >1 uphill, <1 on
    gentle downhills (with the well-known rise again on steep descents).
    """
    i = grade_pct / 100.0
    cr = (
        155.4 * i**5
        - 30.4 * i**4
        - 43.3 * i**3
        + 46.3 * i**2
        + 19.5 * i
        + 3.6
    )
    factor = cr / _FLAT_COST
    lo, hi = _COST_FACTOR_BOUNDS
    return max(lo, min(hi, factor))


def build_pace_model(
    pbs: list[str] | None = None,
    past_run_gpx: list[str | Path] | None = None,
    target_distance_m: float | None = None,
    cadence_override: float | None = None,
) -> PaceModel:
    """Combine manual PBs and/or past-run GPX into a :class:`PaceModel`.

    Priority for base speed: past-run measured average (if provided) is averaged
    with the Riegel prediction from the closest-distance PB; whichever is given is
    used. Cadence: override > measured-from-GPX > default.
    """
    speeds: list[float] = []
    measured_cadence: float | None = None

    if past_run_gpx:
        for gpx_path in past_run_gpx:
            speed, cadence = _measure_from_gpx(gpx_path)
            if speed:
                speeds.append(speed)
            if cadence:
                measured_cadence = cadence

    if pbs and target_distance_m:
        # Use the PB whose distance is closest to the route distance.
        parsed = [parse_pb(s) for s in pbs]
        d_known, t_known = min(parsed, key=lambda dt: abs(dt[0] - target_distance_m))
        predicted_time = riegel_predict(d_known, t_known, target_distance_m)
        speeds.append(target_distance_m / predicted_time)

    if not speeds:
        raise ValueError(
            "Need at least one pace source: pass --pb (with a route GPX) "
            "or --past-run."
        )

    base_speed = sum(speeds) / len(speeds)
    cadence = cadence_override or measured_cadence or _DEFAULT_CADENCE_SPM
    return PaceModel(base_speed_mps=base_speed, cadence_spm=cadence)


def segment_time_s(segment: Segment, model: PaceModel) -> float:
    """Predicted time (s) to cover a segment given its grade and the pace model."""
    if model.base_speed_mps <= 0:
        raise ValueError("base_speed_mps must be positive")
    flat_time = segment.length_m / model.base_speed_mps
    return flat_time * minetti_cost_factor(segment.grade_pct)


def _measure_from_gpx(gpx_path: str | Path) -> tuple[float | None, float | None]:
    """Measure average speed (m/s) and average cadence (spm) from a past run.

    Cadence is read from Garmin TrackPointExtension (<gpxtpx:cad> or <cad>) when
    present; otherwise None.
    """
    path = Path(gpx_path)
    if not path.exists():
        raise FileNotFoundError(f"Past-run GPX not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        gpx = gpxpy.parse(fh)

    total_dist = 0.0
    total_time = 0.0
    cad_values: list[float] = []

    for track in gpx.tracks:
        for seg in track.segments:
            pts = seg.points
            total_dist += seg.length_2d() or 0.0
            for i in range(1, len(pts)):
                if pts[i - 1].time and pts[i].time:
                    total_time += (pts[i].time - pts[i - 1].time).total_seconds()
            for pt in pts:
                cad = _extract_cadence(pt)
                if cad is not None:
                    cad_values.append(cad)

    speed = (total_dist / total_time) if total_time > 0 else None
    cadence = (sum(cad_values) / len(cad_values)) if cad_values else None
    # Cadence in GPX is usually per-leg (one foot). Double if it looks halved.
    if cadence is not None and cadence < 120:
        cadence *= 2
    return speed, cadence


def _extract_cadence(point) -> float | None:
    """Pull a cadence value out of a gpxpy point's extensions, if any."""
    for ext in getattr(point, "extensions", []) or []:
        # ext may be an Element tree; search descendants for a 'cad' tag.
        for child in ext.iter() if hasattr(ext, "iter") else []:
            tag = child.tag.split("}")[-1].lower()
            if tag in ("cad", "cadence") and child.text:
                try:
                    return float(child.text)
                except ValueError:
                    pass
    return None
