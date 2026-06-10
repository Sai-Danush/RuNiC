from pathlib import Path

from runic.effort import build_effort_timeline, dominant_target
from runic.gpx import parse_route
from runic.models import Terrain
from runic.pace import build_pace_model

FIXTURE = Path(__file__).parent / "fixtures" / "sample_route.gpx"


def _setup():
    route = parse_route(FIXTURE, segment_length_m=200.0, smooth_window=1)
    model = build_pace_model(pbs=["5k=25:00"], target_distance_m=route.total_distance_m)
    slots, total = build_effort_timeline(route, model)
    return slots, total


def test_timeline_is_contiguous_and_positive():
    slots, total = _setup()
    assert total > 0
    assert slots[0].start_s == 0.0
    for a, b in zip(slots, slots[1:]):
        assert abs(a.end_s - b.start_s) < 1e-6
    assert abs(slots[-1].end_s - total) < 1e-6


def test_climb_targets_more_energy_than_descent():
    slots, _ = _setup()
    up = [s.target_energy for s in slots if s.terrain in (Terrain.UP, Terrain.STEEP_UP)]
    down = [s.target_energy for s in slots if s.terrain in (Terrain.DOWN, Terrain.STEEP_DOWN)]
    assert up and down
    assert max(up) > max(down)


def test_dominant_target_picks_overlapping_terrain():
    slots, total = _setup()
    # Window over the whole run returns a valid terrain + averaged targets.
    terrain, energy, tempo = dominant_target(slots, 0.0, total)
    assert isinstance(terrain, Terrain)
    assert 0.0 <= energy <= 1.0
    assert tempo > 0
