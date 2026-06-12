"""Hybrid matcher: place songs along the effort timeline.

Score = w_tempo * tempo_fit + w_energy * energy_fit (+ small mood terms), where
tempo_fit is octave-aware (a song at half/double cadence still "fits"). A clock
walks the run; at each step we pick the best unused song for the terrain over the
window that song would occupy, then advance by its duration.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .effort import dominant_target
from .models import EffortSlot, PlaylistEntry, Song, Terrain

# A song that only matches the cadence at half/double time (e.g. 87 BPM for a
# 170 stride) syncs mathematically but *feels* slower. Score those octave
# matches below a true on-tempo match so genuine on-cadence songs win.
_OCTAVE_PENALTY = 0.6


@dataclass(frozen=True)
class Weights:
    """Tunable knobs for the matching feel."""

    tempo: float = 0.5          # beat-sync importance
    energy: float = 0.4         # effort-matching importance
    valence: float = 0.05       # mild positivity nudge
    danceability: float = 0.05  # mild groove nudge
    tempo_tolerance: float = 12.0   # BPM; how forgiving tempo matching is


def octave_tempo_distance(song_tempo: float, target_tempo: float) -> float:
    """Smallest BPM distance allowing half/double-time equivalence."""
    if song_tempo <= 0:
        return abs(target_tempo)
    candidates = (song_tempo, song_tempo * 2, song_tempo / 2)
    return min(abs(c - target_tempo) for c in candidates)


def tempo_fit(song_tempo: float, target_tempo: float, tolerance: float) -> float:
    """Octave-aware tempo fit, but penalising half/double-time matches.

    A direct tempo match scores up to 1.0; a match that only works at half or
    double time tops out at ``_OCTAVE_PENALTY`` so a true on-tempo song is
    always preferred when both are available.
    """
    if song_tempo <= 0:
        return 0.0
    direct = max(0.0, 1.0 - abs(song_tempo - target_tempo) / tolerance)
    octave = max(
        max(0.0, 1.0 - abs(song_tempo * 2 - target_tempo) / tolerance),
        max(0.0, 1.0 - abs(song_tempo / 2 - target_tempo) / tolerance),
    )
    return max(direct, _OCTAVE_PENALTY * octave)


def energy_fit(song_energy: float, target_energy: float) -> float:
    return max(0.0, 1.0 - abs(song_energy - target_energy))


def score_song(
    song: Song, target_energy: float, target_tempo: float, w: Weights
) -> float:
    return (
        w.tempo * tempo_fit(song.tempo, target_tempo, w.tempo_tolerance)
        + w.energy * energy_fit(song.energy, target_energy)
        + w.valence * song.valence
        + w.danceability * song.danceability
    )


def _reason(terrain: Terrain, song: Song, tgt_energy: float, tgt_tempo: float) -> str:
    beat = octave_tempo_distance(song.tempo, tgt_tempo)
    beat_note = "on-cadence" if beat <= 12 else "off-cadence"
    return (
        f"{terrain.value}: energy {song.energy:.2f} vs target {tgt_energy:.2f}, "
        f"{beat_note} ({song.tempo:.0f}≈{tgt_tempo:.0f} BPM)"
    )


def _best_unused(
    pool: list[Song], used: set[str], tgt_e: float, tgt_t: float, w: Weights
) -> Song | None:
    """Highest-scoring song not yet used for a given energy/tempo target."""
    best: tuple[float, Song] | None = None
    for song in pool:
        if song.reccobeats_id in used:
            continue
        s = score_song(song, tgt_e, tgt_t, w)
        if best is None or s > best[0]:
            best = (s, song)
    return best[1] if best else None


def build_playlist(
    slots: list[EffortSlot],
    total_run_s: float,
    songs: list[Song],
    *,
    weights: Weights | None = None,
) -> list[PlaylistEntry]:
    """Fit the run timeline with the best-matching songs.

    Unlike a pure left-to-right greedy walk (which lets early flat slots eat the
    songs the demanding sections need), this assigns songs to time-buckets
    *most-demanding first*: buckets whose target energy sits furthest from the
    pool's average get first pick. Steep descents (attack) and recovery climbs
    are the pickiest, so they're served before generic flat sections. Songs are
    then emitted in time order and trimmed to the predicted run length.
    """
    w = weights or Weights()
    pool = [s for s in songs if s.duration_ms > 0]
    if not pool or total_run_s <= 0:
        return []

    # 1. Slice the run into ordered time-buckets sized by a representative song.
    rep = max(60.0, statistics.median(s.duration_s for s in pool))
    buckets: list[dict] = []
    t = 0.0
    while t < total_run_s:
        end = min(t + rep, total_run_s)
        terrain, tgt_e, tgt_t = dominant_target(slots, t, end)
        buckets.append({"terrain": terrain, "tgt_e": tgt_e, "tgt_t": tgt_t})
        t = end

    # 2. Assign songs to buckets, most-demanding (most extreme energy) first.
    pool_mean_e = statistics.fmean(s.energy for s in pool)
    demand_order = sorted(
        range(len(buckets)),
        key=lambda i: abs(buckets[i]["tgt_e"] - pool_mean_e),
        reverse=True,
    )
    used: set[str] = set()
    assigned: dict[int, Song] = {}
    for i in demand_order:
        b = buckets[i]
        song = _best_unused(pool, used, b["tgt_e"], b["tgt_t"], w)
        if song is None:
            break
        assigned[i] = song
        used.add(song.reccobeats_id)

    # 3. Emit in time order with actual durations; recompute each window's
    #    terrain/targets for an honest "why". Stop once the run is covered.
    entries: list[PlaylistEntry] = []
    clock = 0.0
    order = 1

    def _emit(song: Song) -> None:
        nonlocal clock, order
        end = clock + song.duration_s
        terrain, tgt_e, tgt_t = dominant_target(slots, clock, end)
        entries.append(
            PlaylistEntry(
                order=order, song=song, start_s=clock, end_s=end,
                terrain=terrain, reason=_reason(terrain, song, tgt_e, tgt_t),
            )
        )
        clock = end
        order += 1

    for i in range(len(buckets)):
        if clock >= total_run_s:
            break
        song = assigned.get(i)
        if song is not None:
            _emit(song)

    # 4. Fallback: real durations may fall short of the run — keep filling with
    #    the best remaining songs so the playlist always covers the distance.
    while clock < total_run_s and len(used) < len(pool):
        _, tgt_e, tgt_t = dominant_target(slots, clock, clock + rep)
        song = _best_unused(pool, used, tgt_e, tgt_t, w)
        if song is None:
            break
        used.add(song.reccobeats_id)
        _emit(song)

    return entries
