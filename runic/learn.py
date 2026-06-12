"""Personalized matching: learn what *you* like from 👍/👎 on generated songs.

The matcher ranks candidate songs by a linear score over audio-feature parameters
([matcher.score_song](matcher.py)). Those coefficients are normally hand-tuned
constants. This module learns them from your own ratings instead.

The trick: a song's score is the same linear form as the *logit* of a logistic
regression, ``P(👍) = σ(Σ wᵢ·featureᵢ)``. So fitting a logistic regression to your
👍/👎 history yields coefficients that are themselves the engine's ranking weights —
ranking by score becomes ranking by "most likely to earn a thumbs-up". The model is
purely feature-based: it never sees a song's identity, only its audio parameters, so
it generalizes to songs it has never encountered.

Single user, local files. Feedback rows accumulate in ``.runic_feedback.jsonl``; the
fitted weights are cached in ``.runic_weights.json``. Retraining is cheap, so the web
layer refits the whole history on each generate.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .matcher import energy_fit, tempo_fit
from .models import Song, Terrain

# Ordered feature names. The vector the model learns over (and the matcher scores
# with) is always built in this order.
FEATURES: tuple[str, ...] = (
    "tempo_fit",        # octave-aware cadence match vs the terrain's target tempo
    "energy_fit",       # how close the song's energy is to the terrain's target
    "valence",
    "danceability",
    "loudness",         # normalized from dB into ~0..1
    "acousticness",
    "instrumentalness",
    "speechiness",
    "liveness",
)

# The current hand-tuned weights, used as the cold-start prior. New factors start at
# 0 and only earn weight once your ratings justify it, so an un-trained profile ranks
# exactly like the engine does today.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "tempo_fit": 0.5, "energy_fit": 0.4, "valence": 0.05, "danceability": 0.05,
    "loudness": 0.0, "acousticness": 0.0, "instrumentalness": 0.0,
    "speechiness": 0.0, "liveness": 0.0,
}

TOL = 12.0              # tempo tolerance (BPM) — matches Weights.tempo_tolerance

# Learning thresholds (tunable). More factors ⇒ a bit data-hungrier; shrinkage keeps
# sparse terrains/early data harmless.
MIN_GLOBAL = 30        # ratings before any personalization kicks in
MIN_TERRAIN = 20       # ratings in a terrain before it gets its own (shrunk) model
SHRINK_K = 20          # per-terrain shrinkage toward global: α = n / (n + K)
GLOBAL_SHRINK_K = 30   # global shrinkage toward the defaults prior
C_REG = 1.0            # inverse L2 strength for the logistic fit

# Exploration: sample among the top-K candidates with a temperature that decays as
# ratings accumulate, so early on the list surfaces varied songs to rate (and the
# model doesn't go confidently blind), later converging to your proven favorites.
EXPLORE_T0 = 0.25
EXPLORE_N0 = 40.0
EXPLORE_MIN = 0.0


# --- Feature extraction -------------------------------------------------------

def _clip01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def feature_vector(
    song: Song, target_energy: float, target_tempo: float, tol: float = TOL
) -> dict[str, float]:
    """The normalized (~0..1) feature vector for one song in a terrain context.

    Single source of truth used by both training and scoring, so the two can never
    drift apart.
    """
    return {
        "tempo_fit": tempo_fit(song.tempo, target_tempo, tol),
        "energy_fit": energy_fit(song.energy, target_energy),
        "valence": _clip01(song.valence),
        "danceability": _clip01(song.danceability),
        "loudness": _clip01((song.loudness + 60.0) / 60.0),
        "acousticness": _clip01(song.acousticness),
        "instrumentalness": _clip01(song.instrumentalness),
        "speechiness": _clip01(song.speechiness),
        "liveness": _clip01(song.liveness),
    }


def default_weights() -> dict[str, float]:
    return dict(_DEFAULT_WEIGHTS)


# --- Profile (the learned model) ----------------------------------------------

@dataclass
class Profile:
    """A trained (or cold-start) set of ranking weights."""

    personalized: bool
    global_weights: dict[str, float]
    terrain_weights: dict[str, dict[str, float]]   # terrain.value -> weights
    counts: dict[str, int] = field(default_factory=dict)
    trained_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "personalized": self.personalized,
            "global_weights": self.global_weights,
            "terrain_weights": self.terrain_weights,
            "counts": self.counts,
            "trained_at": self.trained_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(
            personalized=bool(d.get("personalized")),
            global_weights=d.get("global_weights") or default_weights(),
            terrain_weights=d.get("terrain_weights") or {},
            counts=d.get("counts") or {},
            trained_at=float(d.get("trained_at") or 0.0),
        )


def _cold_profile(n_total: int = 0) -> Profile:
    return Profile(
        personalized=False,
        global_weights=default_weights(),
        terrain_weights={},
        counts={"_total": n_total},
        trained_at=time.time(),
    )


def _terrain_key(terrain) -> str:
    return terrain.value if isinstance(terrain, Terrain) else str(terrain)


def weights_for_terrain(profile: Profile, terrain) -> dict[str, float]:
    """Weights for a terrain: its own (if learned) else global else defaults."""
    key = _terrain_key(terrain)
    return (
        profile.terrain_weights.get(key)
        or profile.global_weights
        or default_weights()
    )


# --- Training -----------------------------------------------------------------

def _normalize(raw: dict[str, float]) -> dict[str, float]:
    """Scale weights to unit L1 norm, preserving sign (a negative weight means the
    factor pushes a song *down* — e.g. you dislike speechy tracks here). Degenerate
    all-zero fits fall back to the defaults."""
    total = sum(abs(v) for v in raw.values())
    if total <= 1e-12:
        return default_weights()
    return {k: raw.get(k, 0.0) / total for k in FEATURES}


def _blend(a: dict[str, float], b: dict[str, float], alpha: float) -> dict[str, float]:
    """Convex blend α·a + (1-α)·b, renormalized to unit L1."""
    mixed = {k: alpha * a.get(k, 0.0) + (1.0 - alpha) * b.get(k, 0.0) for k in FEATURES}
    return _normalize(mixed)


def _fit_weights(feat_rows: list[dict[str, float]], labels: list[int]) -> dict[str, float] | None:
    """Fit a logistic regression; return normalized weights, or None if unfittable
    (only one class present, or sklearn unavailable)."""
    if len(set(labels)) < 2:
        return None
    try:
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return None
    x = [[row.get(k, 0.0) for k in FEATURES] for row in feat_rows]
    clf = LogisticRegression(class_weight="balanced", C=C_REG, max_iter=1000)
    clf.fit(x, labels)
    raw = {k: float(c) for k, c in zip(FEATURES, clf.coef_[0])}
    return _normalize(raw)


def train_profile(events: list[dict]) -> Profile:
    """Fit a personalized :class:`Profile` from labeled feedback rows.

    Each event: ``{"features": {<FEATURES>: float}, "terrain": str, "label": 0|1}``.
    Cold start (too little data, or one-sided ratings) returns the defaults, so the
    playlist is unchanged from today until there's enough signal.
    """
    rows = [
        (e["features"], int(e["label"]), e.get("terrain"))
        for e in events
        if isinstance(e.get("features"), dict) and e.get("label") is not None
    ]
    n_total = len(rows)
    if n_total < MIN_GLOBAL:
        return _cold_profile(n_total)

    global_fit = _fit_weights([r[0] for r in rows], [r[1] for r in rows])
    if global_fit is None:
        return _cold_profile(n_total)

    # Shrink the global fit toward the defaults prior (smooth, not a hard switch).
    alpha_g = n_total / (n_total + GLOBAL_SHRINK_K)
    global_weights = _blend(global_fit, default_weights(), alpha_g)

    counts: dict[str, int] = {"_total": n_total}
    terrain_weights: dict[str, dict[str, float]] = {}
    terrains = {r[2] for r in rows if r[2]}
    for terr in terrains:
        subset = [(f, y) for f, y, t in rows if t == terr]
        counts[terr] = len(subset)
        if len(subset) < MIN_TERRAIN:
            continue                       # too sparse → fall back to global
        fit = _fit_weights([f for f, _ in subset], [y for _, y in subset])
        if fit is None:
            continue
        alpha = len(subset) / (len(subset) + SHRINK_K)
        terrain_weights[terr] = _blend(fit, global_weights, alpha)

    return Profile(
        personalized=True,
        global_weights=global_weights,
        terrain_weights=terrain_weights,
        counts=counts,
        trained_at=time.time(),
    )


# --- Scoring + exploration for the matcher ------------------------------------

def score_fn_for(profile: Profile, tol: float = TOL):
    """A ``score_fn(song, tgt_e, tgt_t, terrain) -> float`` for ``build_playlist``,
    or ``None`` when the profile isn't personalized yet (so the matcher uses its own
    default scoring and behaves exactly like today)."""
    if not profile.personalized:
        return None

    def score(song: Song, tgt_e: float, tgt_t: float, terrain) -> float:
        fv = feature_vector(song, tgt_e, tgt_t, tol)
        w = weights_for_terrain(profile, terrain)
        return sum(fv[k] * w.get(k, 0.0) for k in FEATURES)

    return score


def exploration_temp(n_ratings: int) -> float:
    """Softmax temperature for top-K candidate sampling; decays as ratings grow."""
    return max(EXPLORE_MIN, EXPLORE_T0 / (1.0 + n_ratings / EXPLORE_N0))


# --- Persistence (local files, single user) -----------------------------------

def _data_dir() -> Path:
    return Path(os.environ.get("RUNIC_DATA_DIR") or ".")


def _feedback_path() -> Path:
    return _data_dir() / ".runic_feedback.jsonl"


def _weights_path() -> Path:
    return _data_dir() / ".runic_weights.json"


def append_event(features: dict[str, float], terrain, label: int,
                 spotify_id: str = "") -> None:
    """Append one labeled rating to the feedback log."""
    row = {
        "ts": time.time(),
        "spotify_id": spotify_id,
        "terrain": _terrain_key(terrain),
        "features": {k: float(features.get(k, 0.0)) for k in FEATURES},
        "label": int(label),
    }
    with _feedback_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def load_events() -> list[dict]:
    path = _feedback_path()
    if not path.exists():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # tolerate a torn final line
    return events


def n_ratings() -> int:
    return len(load_events())


def save_profile(profile: Profile) -> None:
    _weights_path().write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")


def load_profile() -> Profile | None:
    path = _weights_path()
    if not path.exists():
        return None
    try:
        return Profile.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def train_from_log() -> Profile:
    """Refit from the full feedback log and cache the result. Source of truth is the
    log; the weights file is just a cache for inspection."""
    profile = train_profile(load_events())
    try:
        save_profile(profile)
    except OSError:
        pass                  # caching is best-effort
    return profile


def reset() -> None:
    """Forget everything — clear the feedback log and cached weights."""
    for path in (_feedback_path(), _weights_path()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
