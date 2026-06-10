"""Read the track list of a PUBLIC Spotify playlist.

Spotify's Web API no longer returns playlist *contents* to app-only
(Client-Credentials) tokens — /playlists/{id}/tracks responds 403 and the
embedded ``tracks`` field comes back null. The public **embed page**, however,
exposes the full track list as JSON and needs no authentication, so that is what
we use. Only track IDs are needed; all metadata + audio features come from
ReccoBeats.
"""

from __future__ import annotations

import json
import re
import time

import requests

_EMBED_URL = "https://open.spotify.com/embed/playlist/{id}"
_TIMEOUT = 20
_RETRIES = 3
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
_PLAYLIST_RE = re.compile(r"playlist[/:]([A-Za-z0-9]+)")


def extract_playlist_id(ref: str) -> str:
    """Normalise a playlist URL / URI / raw id to a bare playlist id."""
    ref = ref.strip()
    match = _PLAYLIST_RE.search(ref)
    if match:
        return match.group(1)
    return ref.split("?")[0]


def _find_key(obj, key):
    """Depth-first search for the first value under ``key`` in nested JSON."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def get_playlist_track_ids(playlist_ref: str) -> list[str]:
    """Return the Spotify track IDs of a public playlist via its embed page.

    Note: the embed payload may cap very large playlists; for typical playlists
    it returns the full list. Non-track entries (episodes/local) are skipped.
    """
    playlist_id = extract_playlist_id(playlist_ref)
    url = _EMBED_URL.format(id=playlist_id)
    resp = None
    for attempt in range(_RETRIES):
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=_TIMEOUT)
        if resp.status_code == 200:
            break
        if resp.status_code in (429, 500, 502, 503, 504):  # transient — retry
            time.sleep(1.0 * (attempt + 1))
            continue
        break  # other statuses are not retryable
    if resp is None or resp.status_code != 200:
        code = resp.status_code if resp is not None else "no response"
        raise RuntimeError(
            f"Could not load playlist embed ({code}) for {playlist_ref}. "
            "Is the playlist public? (Spotify embed may be temporarily down.)"
        )

    match = _NEXT_DATA_RE.search(resp.text)
    if not match:
        raise RuntimeError(
            f"Unexpected embed format for {playlist_ref} — no track data found."
        )
    data = json.loads(match.group(1))
    track_list = _find_key(data, "trackList") or []

    ids: list[str] = []
    for entry in track_list:
        uri = entry.get("uri", "") if isinstance(entry, dict) else ""
        if uri.startswith("spotify:track:"):
            ids.append(uri.split(":")[-1])
    if not ids:
        raise RuntimeError(
            f"No track IDs found in playlist {playlist_ref}. "
            "It may be empty, private, or contain only non-track items."
        )
    return ids


def collect_candidate_ids(playlist_refs: list[str]) -> list[str]:
    """Read every playlist and return a de-duplicated id pool (no auth needed)."""
    seen: dict[str, None] = {}
    for ref in playlist_refs:
        for track_id in get_playlist_track_ids(ref):
            seen.setdefault(track_id, None)
    return list(seen.keys())
