import json
from pathlib import Path

from runic.matcher import (
    Weights,
    build_playlist,
    octave_tempo_distance,
    score_song,
    tempo_fit,
)
from runic.models import EffortSlot, Song, Terrain

FIXTURE = Path(__file__).parent / "fixtures" / "candidates.json"


def _load_songs() -> list[Song]:
    data = json.loads(FIXTURE.read_text())
    return [
        Song(
            spotify_id=d["spotify_id"], reccobeats_id=d["reccobeats_id"],
            title=d["title"], artists=tuple(d["artists"]),
            duration_ms=d["duration_ms"], tempo=d["tempo"], energy=d["energy"],
            valence=d["valence"], danceability=d["danceability"], loudness=d["loudness"],
        )
        for d in data
    ]


def _song(songs, sid) -> Song:
    return next(s for s in songs if s.spotify_id == sid)


def test_octave_distance_handles_halftime():
    # 86 BPM should match a 170 cadence via doubling.
    assert octave_tempo_distance(86.0, 170.0) <= 2.0
    assert octave_tempo_distance(120.0, 170.0) > 10.0


def test_tempo_fit_in_range():
    assert tempo_fit(170.0, 170.0, 12.0) == 1.0
    assert tempo_fit(120.0, 170.0, 12.0) == 0.0


def test_climb_target_prefers_fast_high_energy():
    songs = _load_songs()
    # Climb: high energy + on cadence.
    e_climb, t_climb = 0.90, 172.0
    best = max(songs, key=lambda s: score_song(s, e_climb, t_climb, Weights()))
    assert best.spotify_id == "fast_hi"


def test_energy_flips_preference_with_terrain():
    """Same two on-cadence songs: the high-energy one wins the climb, the
    low-energy one wins the descent. Proves energy drives selection."""
    songs = _load_songs()
    hi = _song(songs, "fast_hi")   # 172 BPM, energy 0.93
    lo = _song(songs, "fast_lo")   # 170 BPM, energy 0.50
    w = Weights()

    # Climb target: high energy preferred.
    assert score_song(hi, 0.90, 172.0, w) > score_song(lo, 0.90, 172.0, w)
    # Descent target: low energy preferred.
    assert score_song(lo, 0.48, 168.0, w) > score_song(hi, 0.48, 168.0, w)


def test_build_playlist_fills_run_and_no_repeats():
    songs = _load_songs()
    # Single steep-up slot long enough for ~3 songs (~10 min).
    slots = [EffortSlot(0.0, 600.0, Terrain.STEEP_UP, 0.90, 172.0)]
    entries = build_playlist(slots, 600.0, songs)
    assert entries
    ids = [e.song.reccobeats_id for e in entries]
    assert len(ids) == len(set(ids))           # no repeats
    assert entries[-1].end_s >= 600.0          # covers the run
    assert entries[0].song.spotify_id == "fast_hi"  # best climb song first
