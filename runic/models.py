"""Shared data models passed between pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Terrain(str, Enum):
    """Coarse terrain class derived from grade."""

    STEEP_UP = "steep_up"
    UP = "up"
    FLAT = "flat"
    DOWN = "down"
    STEEP_DOWN = "steep_down"


@dataclass(frozen=True)
class Segment:
    """A contiguous slice of the route with roughly uniform grade."""

    start_m: float          # cumulative distance at segment start (metres)
    end_m: float            # cumulative distance at segment end (metres)
    grade_pct: float        # average grade over the segment (%)
    terrain: Terrain

    @property
    def length_m(self) -> float:
        return self.end_m - self.start_m


@dataclass(frozen=True)
class RouteProfile:
    """Parsed GPX route: ordered segments + totals."""

    segments: list[Segment]
    total_distance_m: float
    total_ascent_m: float
    total_descent_m: float


@dataclass(frozen=True)
class EffortSlot:
    """A span of the run on the TIME axis with its music targets."""

    start_s: float          # seconds from run start
    end_s: float
    terrain: Terrain
    target_energy: float    # desired song energy (0..1)
    target_tempo: float     # desired song tempo / cadence (BPM)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass(frozen=True)
class Song:
    """A candidate track enriched with ReccoBeats audio features."""

    spotify_id: str
    reccobeats_id: str
    title: str
    artists: tuple[str, ...]
    duration_ms: int
    tempo: float
    energy: float
    valence: float
    danceability: float
    loudness: float

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def artist_str(self) -> str:
        return ", ".join(self.artists)


@dataclass
class PlaylistEntry:
    """One placed song in the final ordered playlist."""

    order: int
    song: Song
    start_s: float          # when this song starts in the run
    end_s: float
    terrain: Terrain        # dominant terrain over the song's window
    reason: str = ""        # short human-readable "why"
    extras: dict = field(default_factory=dict)
