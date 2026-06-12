"""ReccoBeats client: resolve Spotify track IDs to songs with audio features.

No API key required. Two batched endpoints:
  * GET /v1/track?ids=<spotify ids>      -> ReccoBeats id + title/artists/duration
  * GET /v1/audio-features?ids=<rb ids>  -> tempo/energy/valence/etc.
"""

from __future__ import annotations

import time

import requests

from .models import Song

_BASE = "https://api.reccobeats.com/v1"
_CHUNK = 40                # ids per request (endpoint accepts comma-separated)
_TIMEOUT = 20
_RETRIES = 3


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _get(session: requests.Session, path: str, ids: list[str]) -> list[dict]:
    """GET a batched endpoint with simple retry; returns the 'content' list."""
    url = f"{_BASE}{path}"
    params = {"ids": ",".join(ids)}
    last_err: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            resp = session.get(url, params=params, timeout=_TIMEOUT,
                               headers={"Accept": "application/json"})
            if resp.status_code == 429:  # rate limited
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json().get("content", [])
        except requests.RequestException as exc:
            last_err = exc
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"ReccoBeats request failed for {path}: {last_err}")


def fetch_songs(spotify_ids: list[str]) -> tuple[list[Song], list[str]]:
    """Resolve Spotify track IDs into :class:`Song`s with audio features.

    Returns ``(songs, skipped_spotify_ids)`` — tracks absent from the ReccoBeats
    catalogue (or missing features) are reported in the second list, not dropped
    silently.
    """
    spotify_ids = list(dict.fromkeys(spotify_ids))  # de-dupe, keep order
    if not spotify_ids:
        return [], []

    session = requests.Session()

    # 1) Spotify id -> ReccoBeats metadata.
    rb_meta: dict[str, dict] = {}            # reccobeats_id -> meta
    spotify_to_rb: dict[str, str] = {}       # spotify_id -> reccobeats_id
    for chunk in _chunks(spotify_ids, _CHUNK):
        for item in _get(session, "/track", chunk):
            rb_id = item.get("id")
            href = item.get("href", "") or ""
            spotify_id = href.rstrip("/").split("/")[-1] if href else None
            if not rb_id or not spotify_id:
                continue
            rb_meta[rb_id] = item
            spotify_to_rb[spotify_id] = rb_id

    # 2) ReccoBeats id -> audio features.
    features: dict[str, dict] = {}
    rb_ids = list(rb_meta.keys())
    for chunk in _chunks(rb_ids, _CHUNK):
        for item in _get(session, "/audio-features", chunk):
            rb_id = item.get("id")
            if rb_id:
                features[rb_id] = item

    # 3) Stitch together; record anything we couldn't fully resolve.
    songs: list[Song] = []
    skipped: list[str] = []
    for spotify_id in spotify_ids:
        rb_id = spotify_to_rb.get(spotify_id)
        feat = features.get(rb_id) if rb_id else None
        meta = rb_meta.get(rb_id) if rb_id else None
        if not (rb_id and feat and meta):
            skipped.append(spotify_id)
            continue
        artists = tuple(a.get("name", "") for a in meta.get("artists", []))
        songs.append(
            Song(
                spotify_id=spotify_id,
                reccobeats_id=rb_id,
                title=meta.get("trackTitle", "Unknown"),
                artists=artists or ("Unknown",),
                duration_ms=int(meta.get("durationMs") or 0),
                tempo=float(feat.get("tempo") or 0.0),
                energy=float(feat.get("energy") or 0.0),
                valence=float(feat.get("valence") or 0.0),
                danceability=float(feat.get("danceability") or 0.0),
                loudness=float(feat.get("loudness") or 0.0),
                acousticness=float(feat.get("acousticness") or 0.0),
                instrumentalness=float(feat.get("instrumentalness") or 0.0),
                speechiness=float(feat.get("speechiness") or 0.0),
                liveness=float(feat.get("liveness") or 0.0),
            )
        )
    return songs, skipped
