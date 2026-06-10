from pathlib import Path

from runic.gpx import parse_route
from runic.models import Terrain

FIXTURE = Path(__file__).parent / "fixtures" / "sample_route.gpx"


def test_total_distance_is_about_1200m():
    route = parse_route(FIXTURE, segment_length_m=200.0, smooth_window=1)
    # 6 spans of ~200 m at the equator.
    assert 1150 < route.total_distance_m < 1260


def test_segments_cover_full_route():
    route = parse_route(FIXTURE, segment_length_m=200.0, smooth_window=1)
    assert route.segments[0].start_m == 0.0
    assert abs(route.segments[-1].end_m - route.total_distance_m) < 1e-6
    # Contiguous, non-overlapping.
    for a, b in zip(route.segments, route.segments[1:]):
        assert abs(a.end_m - b.start_m) < 1e-6


def test_terrain_classes_present():
    route = parse_route(FIXTURE, segment_length_m=200.0, smooth_window=1)
    terrains = {seg.terrain for seg in route.segments}
    # Route climbs then descends, so we expect both up and down classes.
    assert terrains & {Terrain.UP, Terrain.STEEP_UP}
    assert terrains & {Terrain.DOWN, Terrain.STEEP_DOWN}


def test_ascent_and_descent_positive():
    route = parse_route(FIXTURE, segment_length_m=200.0, smooth_window=1)
    assert route.total_ascent_m > 0
    assert route.total_descent_m > 0
