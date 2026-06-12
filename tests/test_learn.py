import json

import pytest

from runic import learn
from runic.matcher import energy_fit, tempo_fit
from runic.models import Song, Terrain


def _song(**kw) -> Song:
    base = dict(
        spotify_id="s", reccobeats_id="r", title="T", artists=("A",),
        duration_ms=200000, tempo=170.0, energy=0.7, valence=0.5,
        danceability=0.6, loudness=-6.0, acousticness=0.1,
        instrumentalness=0.0, speechiness=0.05, liveness=0.2,
    )
    base.update(kw)
    return Song(**base)


def _event(label, terrain="flat", **feat):
    fv = {k: 0.5 for k in learn.FEATURES}
    fv.update(feat)
    return {"features": fv, "terrain": terrain, "label": label}


# --- feature extraction -------------------------------------------------------

def test_feature_vector_matches_matcher_and_is_normalized():
    song = _song(tempo=170.0, energy=0.9)
    fv = learn.feature_vector(song, target_energy=0.9, target_tempo=170.0)
    # All declared features present.
    assert set(fv) == set(learn.FEATURES)
    # Context fits agree with the matcher's own functions; perfect match => 1.0.
    assert fv["tempo_fit"] == tempo_fit(170.0, 170.0, learn.TOL) == 1.0
    assert fv["energy_fit"] == energy_fit(0.9, 0.9) == 1.0
    # loudness normalized into 0..1; raw params clipped to 0..1.
    assert 0.0 <= fv["loudness"] <= 1.0
    assert fv["acousticness"] == 0.1


# --- cold start ---------------------------------------------------------------

def test_cold_start_returns_defaults():
    profile = learn.train_profile([_event(1), _event(0)])  # well below MIN_GLOBAL
    assert profile.personalized is False
    assert profile.global_weights == learn.default_weights()
    # No personalized scorer => matcher uses its own default path.
    assert learn.score_fn_for(profile) is None


# --- learning a brand-new factor ---------------------------------------------

def test_learns_new_factor_from_ratings():
    # 👍 songs are acoustic, 👎 songs are not; everything else is identical. The
    # model should put clear positive weight on acousticness — a factor whose
    # default weight is 0 and that the old engine ignored entirely.
    events = [_event(1, acousticness=0.9) for _ in range(20)]
    events += [_event(0, acousticness=0.1) for _ in range(20)]
    profile = learn.train_profile(events)

    assert profile.personalized is True
    w = profile.global_weights
    assert w["acousticness"] > 0.3
    # It dominates the constant-valued features (which carry no signal here).
    assert w["acousticness"] > w["tempo_fit"]

    # And the learned scorer ranks the acoustic song above the non-acoustic one.
    score = learn.score_fn_for(profile)
    acoustic = _song(acousticness=0.9)
    electronic = _song(acousticness=0.1)
    assert score(acoustic, 0.7, 170.0, Terrain.FLAT) > \
        score(electronic, 0.7, 170.0, Terrain.FLAT)


def test_per_terrain_weights_and_counts():
    # Enough flat-terrain rows to earn a terrain-specific (shrunk) model.
    events = [_event(1, terrain="flat", acousticness=0.9) for _ in range(20)]
    events += [_event(0, terrain="flat", acousticness=0.1) for _ in range(20)]
    profile = learn.train_profile(events)
    assert profile.counts["_total"] == 40
    assert profile.counts["flat"] == 40
    assert "flat" in profile.terrain_weights
    # A terrain with no rows falls back to the global weights.
    assert learn.weights_for_terrain(profile, Terrain.STEEP_UP) == profile.global_weights


# --- exploration --------------------------------------------------------------

def test_exploration_temp_decays():
    assert learn.exploration_temp(0) > learn.exploration_temp(50) > learn.exploration_temp(500)
    assert learn.exploration_temp(10_000) >= learn.EXPLORE_MIN


# --- persistence --------------------------------------------------------------

def test_persistence_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNIC_DATA_DIR", str(tmp_path))
    assert learn.n_ratings() == 0

    learn.append_event({k: 0.5 for k in learn.FEATURES}, Terrain.UP, 1, spotify_id="abc")
    events = learn.load_events()
    assert len(events) == 1 and events[0]["label"] == 1 and events[0]["terrain"] == "up"
    assert learn.n_ratings() == 1

    profile = learn.Profile(personalized=True, global_weights=learn.default_weights(),
                            terrain_weights={}, counts={"_total": 1})
    learn.save_profile(profile)
    loaded = learn.load_profile()
    assert loaded is not None and loaded.personalized is True

    learn.reset()
    assert learn.n_ratings() == 0
    assert learn.load_profile() is None


# --- ReccoBeats now captures the extra factors --------------------------------

def test_fetch_songs_maps_extra_factors(monkeypatch):
    from runic import reccobeats

    def fake_get(session, path, ids):
        if path == "/track":
            return [{"id": "rb1", "href": "https://open.spotify.com/track/spot1",
                     "trackTitle": "Song", "artists": [{"name": "Artist"}],
                     "durationMs": 210000}]
        return [{"id": "rb1", "tempo": 168.0, "energy": 0.8, "valence": 0.4,
                 "danceability": 0.6, "loudness": -5.0, "acousticness": 0.12,
                 "instrumentalness": 0.003, "speechiness": 0.07, "liveness": 0.25}]

    monkeypatch.setattr(reccobeats, "_get", fake_get)
    songs, skipped = reccobeats.fetch_songs(["spot1"])
    assert not skipped and len(songs) == 1
    s = songs[0]
    assert s.acousticness == 0.12
    assert s.instrumentalness == 0.003
    assert s.speechiness == 0.07
    assert s.liveness == 0.25
