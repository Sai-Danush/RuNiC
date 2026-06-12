"""FastAPI app wrapping the Runic engine.

Flow: Spotify auth (read scopes) -> list the user's PUBLIC playlists -> upload a
GPX (parsed into an elevation profile + terrain segments) -> generate a
terrain-matched ordered playlist from the chosen playlists' songs.

Spotify constraints baked in here:
  * Listing playlists (``GET /me/playlists``) works with a user token.
  * Reading a playlist's *tracks* via the Web API is blocked (403) for dev-mode
    apps, so song fetching goes through the public embed page
    (``runic.spotify_client``) — which only works for PUBLIC playlists.
  * Writing a playlist back to the account is blocked too, so output is the
    ordered list + copy-ready Spotify track links, not a saved playlist.

Run with: ``runic-web`` (or ``uvicorn runic.web.app:app``). Serves on
127.0.0.1:8888 so the already-registered ``/callback`` redirect URI matches.
"""

from __future__ import annotations

import base64
import os
import secrets
import tempfile
import time
import urllib.parse
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .. import learn
from ..config import ConfigError, load_spotify_credentials
from ..effort import build_effort_timeline, dominant_target
from ..gpx import parse_route
from ..matcher import Weights, build_playlist
from ..models import RouteProfile, Terrain
from ..pace import build_pace_model
from ..reccobeats import fetch_songs
from ..spotify_client import collect_candidate_ids, extract_playlist_id

_AUTH_URL = "https://accounts.spotify.com/authorize"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_API = "https://api.spotify.com/v1"
# Free, keyless routing+elevation: snaps drawn waypoints to footpaths and returns
# GPX with SRTM <ele> tags. Used by the "draw on map" route picker.
_BROUTER_URL = "https://brouter.de/brouter"
# Read scopes for listing playlists + profile, plus playback scopes for the
# in-page Web Playback SDK player (Option 1 feasibility probe).
_SCOPES = (
    "playlist-read-private playlist-read-collaborative user-read-private "
    "streaming user-modify-playback-state user-read-playback-state"
)

# A widely-available track for the playback feasibility test (Blinding Lights).
_TEST_TRACK_URI = "spotify:track:0VjIjW4GlUZAMYd2vXMi3b"
# Host/port: defaults suit local dev; deploy platforms inject PORT (and you set
# HOST=0.0.0.0). The OAuth callback defaults to localhost but is overridable so
# a deployed app can point Spotify at its real HTTPS domain.
_HOST = os.environ.get("HOST", "127.0.0.1")
_PORT = int(os.environ.get("PORT", "8888"))
_REDIRECT_URI = os.environ.get(
    "RUNIC_REDIRECT_URI", f"http://127.0.0.1:{_PORT}/callback"
)
_TIMEOUT = 20

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Runic")
app.add_middleware(
    SessionMiddleware,
    # Stable secret in production (set SESSION_SECRET); ephemeral per-process
    # locally — fine for dev, but it logs users out on every restart, so a
    # deployed app should always set SESSION_SECRET.
    secret_key=os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32),
    same_site="lax",
    max_age=60 * 60 * 8,
)

# Server-side per-session state (parsed routes are too big for a cookie).
_ROUTES: dict[str, RouteProfile] = {}
# Per-session feature cache from the last generate: spotify_id -> {features, terrain}.
# Lets /api/feedback turn a 👍/👎 into a labeled training row without the browser
# having to echo back the audio features.
_FEATURES: dict[str, dict[str, dict]] = {}


# --- OAuth helpers ------------------------------------------------------------


def _creds():
    try:
        return load_spotify_credentials()
    except ConfigError as exc:
        raise HTTPException(500, str(exc)) from exc


def _basic_auth(creds) -> dict[str, str]:
    raw = f"{creds.client_id}:{creds.client_secret}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _store_tokens(session: dict, payload: dict) -> None:
    session["access_token"] = payload["access_token"]
    session["expires_at"] = time.time() + payload.get("expires_in", 3600) - 60
    if payload.get("refresh_token"):
        session["refresh_token"] = payload["refresh_token"]


def _valid_token(request: Request) -> str:
    """Return a fresh access token for this session, refreshing if needed."""
    session = request.session
    token = session.get("access_token")
    if token and time.time() < session.get("expires_at", 0):
        return token
    refresh = session.get("refresh_token")
    if not refresh:
        raise HTTPException(401, "Not authenticated with Spotify.")
    resp = requests.post(
        _TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh},
        headers=_basic_auth(_creds()),
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        session.clear()
        raise HTTPException(401, "Spotify session expired — please log in again.")
    payload = resp.json()
    payload.setdefault("refresh_token", refresh)
    _store_tokens(session, payload)
    return payload["access_token"]


def _sid(request: Request) -> str:
    sid = request.session.get("sid")
    if not sid:
        sid = secrets.token_urlsafe(16)
        request.session["sid"] = sid
    return sid


# --- Routes: auth -------------------------------------------------------------


@app.get("/login")
def login(request: Request):
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    query = urllib.parse.urlencode(
        {
            "client_id": _creds().client_id,
            "response_type": "code",
            "redirect_uri": _REDIRECT_URI,
            "scope": _SCOPES,
            "state": state,
        }
    )
    return RedirectResponse(f"{_AUTH_URL}?{query}")


@app.get("/callback")
def callback(request: Request, code: str | None = None,
             state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"/?error={urllib.parse.quote(error)}")
    if not code or state != request.session.get("oauth_state"):
        return RedirectResponse("/?error=state_mismatch")
    resp = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT_URI,
        },
        headers=_basic_auth(_creds()),
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        return RedirectResponse(f"/?error=token_exchange_{resp.status_code}")
    _store_tokens(request.session, resp.json())
    return RedirectResponse("/")


@app.post("/logout")
def logout(request: Request):
    _ROUTES.pop(request.session.get("sid", ""), None)
    request.session.clear()
    return {"ok": True}


# --- Routes: data -------------------------------------------------------------


@app.get("/api/token")
def api_token(request: Request):
    """Hand the current access token to the Web Playback SDK (localhost only)."""
    return {"access_token": _valid_token(request)}


@app.get("/api/playback/devices")
def api_playback_devices(request: Request):
    """Proxy GET /me/player/devices — surfaces whether the playback API is open."""
    token = _valid_token(request)
    resp = requests.get(
        f"{_API}/me/player/devices",
        headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT,
    )
    return {
        "status": resp.status_code,
        "body": resp.json() if resp.content else None,
    }


@app.post("/api/playback/play")
async def api_playback_play(request: Request):
    """Proxy PUT /me/player/play.

    Accepts ``uris`` (an ordered list — the run player sends the whole
    terrain-matched playlist so Spotify handles gapless auto-advance) or a
    single ``uri`` (the feasibility probe). Optional ``offset`` (index) and
    ``position_ms`` resume mid-list.
    """
    token = _valid_token(request)
    body = await request.json()
    device_id = body.get("device_id")
    uris = body.get("uris")
    if not uris:
        uris = [body.get("uri", _TEST_TRACK_URI)]
    payload: dict = {"uris": uris}
    offset = body.get("offset")
    if offset is not None:
        payload["offset"] = {"position": int(offset)}
    if body.get("position_ms") is not None:
        payload["position_ms"] = int(body["position_ms"])
    params = {"device_id": device_id} if device_id else {}
    resp = requests.put(
        f"{_API}/me/player/play",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        params=params,
        json=payload,
        timeout=_TIMEOUT,
    )
    return {
        "status": resp.status_code,
        "ok": resp.status_code in (200, 202, 204),
        "body": resp.text[:300] if resp.content else None,
    }


@app.get("/api/me")
def api_me(request: Request):
    token = _valid_token(request)
    resp = requests.get(
        f"{_API}/me", headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT
    )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, "Could not load Spotify profile.")
    me = resp.json()
    return {"id": me.get("id"), "display_name": me.get("display_name") or me.get("id")}


@app.get("/api/playlists")
def api_playlists(request: Request):
    token = _valid_token(request)
    headers = {"Authorization": f"Bearer {token}"}
    items: list[dict] = []
    url = f"{_API}/me/playlists?limit=50"
    while url:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, "Could not list playlists.")
        data = resp.json()
        for p in data.get("items", []):
            if not p:
                continue
            imgs = p.get("images") or []
            items.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "public": bool(p.get("public")),
                    "collaborative": bool(p.get("collaborative")),
                    "owner": (p.get("owner") or {}).get("display_name", ""),
                    "image": imgs[0]["url"] if imgs else None,
                }
            )
        url = data.get("next")
    # Public playlists first (those are the usable ones), then by name.
    items.sort(key=lambda x: (not x["public"], (x["name"] or "").lower()))
    return {"playlists": items}


def _elevation_points(route: RouteProfile, max_points: int = 200) -> list[dict]:
    """Reconstruct a (distance_km, relative_elevation_m) profile from segment
    grades — faithful to the terrain classification the matcher actually uses."""
    pts: list[dict] = [{"km": 0.0, "ele": 0.0}]
    ele = 0.0
    for seg in route.segments:
        ele += seg.grade_pct / 100.0 * seg.length_m
        pts.append({"km": round(seg.end_m / 1000.0, 3), "ele": round(ele, 1)})
    # Downsample for the chart if very long.
    if len(pts) > max_points:
        step = len(pts) / max_points
        pts = [pts[int(i * step)] for i in range(max_points)] + [pts[-1]]
    return pts


def _route_response(route: RouteProfile) -> dict:
    """Shape a parsed RouteProfile into the JSON the frontend's renderRoute() wants.
    Shared by /api/gpx (uploaded file) and /api/route (drawn on the map) so both
    paths feed the identical analysis + chart."""
    terrain_counts: dict[str, int] = {}
    for seg in route.segments:
        terrain_counts[seg.terrain.value] = terrain_counts.get(seg.terrain.value, 0) + 1
    return {
        "distance_km": round(route.total_distance_m / 1000.0, 2),
        "ascent_m": round(route.total_ascent_m),
        "descent_m": round(route.total_descent_m),
        "n_segments": len(route.segments),
        "elevation": _elevation_points(route),
        "terrain_counts": terrain_counts,
        "segments": [
            {"km": round(s.start_m / 1000.0, 3), "terrain": s.terrain.value}
            for s in route.segments
        ],
    }


def _gpx_latlngs(gpx_text: str, max_points: int = 300) -> list[list[float]]:
    """Extract the track geometry as [[lat, lng], ...] so the map can draw the
    snapped path. Cosmetic only — returns [] if the GPX can't be parsed."""
    try:
        import gpxpy  # lazy: only needed for the map flow

        gpx = gpxpy.parse(gpx_text)
        pts: list[list[float]] = [
            [p.latitude, p.longitude]
            for track in gpx.tracks
            for seg in track.segments
            for p in seg.points
        ]
    except Exception:
        return []
    if len(pts) > max_points:
        step = len(pts) / max_points
        pts = [pts[int(i * step)] for i in range(max_points)] + [pts[-1]]
    return pts


@app.post("/api/gpx")
async def api_gpx(request: Request, file: UploadFile):
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty GPX file.")
    with tempfile.NamedTemporaryFile(suffix=".gpx", delete=True) as tmp:
        tmp.write(raw)
        tmp.flush()
        try:
            route = parse_route(tmp.name)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, f"Could not parse GPX: {exc}") from exc
    _ROUTES[_sid(request)] = route
    return _route_response(route)


@app.post("/api/route")
async def api_route(request: Request):
    """Build a route from waypoints drawn on the map. BRouter snaps the points to
    real footpaths and returns GPX with SRTM elevation; we then run it through the
    same parse_route pipeline as an uploaded GPX."""
    body = await request.json()
    wps = body.get("waypoints") or []  # [[lat, lng], ...]
    if len(wps) < 2:
        raise HTTPException(400, "Pick at least a start and an end point on the map.")
    profile = body.get("profile") or "hiking-beta"  # foot profile (verified working)
    try:
        # BRouter wants lon,lat (longitude FIRST), pipe-separated.
        lonlats = "|".join(f"{float(lng)},{float(lat)}" for lat, lng in wps)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Invalid waypoints.") from exc

    try:
        resp = requests.get(
            _BROUTER_URL,
            params={
                "lonlats": lonlats,
                "profile": profile,
                "alternativeidx": 0,
                "format": "gpx",
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"Routing service unreachable: {exc}") from exc
    # BRouter signals failure with a 500 + plaintext body (no path found, etc.).
    if not resp.ok or "<trkpt" not in resp.text:
        raise HTTPException(
            400, "Couldn't route between those points — try moving them onto a path."
        )

    with tempfile.NamedTemporaryFile(suffix=".gpx", delete=True) as tmp:
        tmp.write(resp.content)
        tmp.flush()
        try:
            route = parse_route(tmp.name)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, f"Could not parse generated route: {exc}") from exc
    _ROUTES[_sid(request)] = route

    return {**_route_response(route), "geometry": _gpx_latlngs(resp.text)}


@app.post("/api/generate")
async def api_generate(request: Request):
    body = await request.json()
    route = _ROUTES.get(_sid(request))
    if route is None:
        raise HTTPException(400, "Upload a GPX route first.")

    playlist_ids = body.get("playlist_ids") or []
    if not playlist_ids:
        raise HTTPException(400, "Select at least one public playlist.")
    pbs = body.get("pbs") or []
    cadence = body.get("cadence")
    if not pbs and not cadence:
        # Need a pace source; a PB is required (no past-run upload in the web UI).
        raise HTTPException(400, "Add at least one personal best (e.g. 5k=22:30).")

    # 1. Collect candidate track ids from the chosen public playlists (embed).
    refs = [extract_playlist_id(pid) for pid in playlist_ids]
    try:
        spotify_ids = collect_candidate_ids(refs)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not spotify_ids:
        raise HTTPException(400, "No tracks found in the selected playlists.")

    # 2. Enrich via ReccoBeats.
    songs, skipped = fetch_songs(spotify_ids)
    if not songs:
        raise HTTPException(
            400, "None of the selected tracks are in the ReccoBeats catalogue."
        )

    # 3. Pace -> effort timeline -> matched playlist.
    try:
        model = build_pace_model(
            pbs=pbs or None,
            target_distance_m=route.total_distance_m,
            cadence_override=float(cadence) if cadence else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    slots, total_s = build_effort_timeline(route, model)

    # Scoring: a manual w_tempo/w_energy override (for debugging) wins; otherwise use
    # the personalized model learned from your 👍/👎 history. Cold start ⇒ score_fn is
    # None ⇒ build_playlist falls back to the default Weights (unchanged behavior).
    manual = any(k in body for k in ("w_tempo", "w_energy", "tempo_tol"))
    weights = Weights(
        tempo=float(body.get("w_tempo", 0.5)),
        energy=float(body.get("w_energy", 0.4)),
        tempo_tolerance=float(body.get("tempo_tol", 12.0)),
    )
    if manual:
        profile = None
        score_fn = None
        explore = 0.0
    else:
        profile = learn.train_from_log()
        score_fn = learn.score_fn_for(profile)
        n_rated = profile.counts.get("_total", 0)
        # "Discovery" (default on) mixes in varied picks so there's something new to
        # rate; exploration decays automatically as ratings accumulate.
        explore = learn.exploration_temp(n_rated) if body.get("discovery", True) else 0.0

    entries = build_playlist(
        slots, total_s, songs, weights=weights, score_fn=score_fn, explore=explore
    )

    # Cache each placed song's features (keyed by spotify id) so a later thumbs-up/down
    # becomes a labeled row without the browser re-sending audio params.
    feat_cache: dict[str, dict] = {}
    for e in entries:
        terrain, tgt_e, tgt_t = dominant_target(slots, e.start_s, e.end_s)
        feat_cache[e.song.spotify_id] = {
            "features": learn.feature_vector(e.song, tgt_e, tgt_t),
            "terrain": terrain.value,
        }
    _FEATURES[_sid(request)] = feat_cache

    personalization = {
        "active": bool(profile and profile.personalized),
        "n_total": (profile.counts.get("_total", 0) if profile else learn.n_ratings()),
        "per_terrain_counts": (
            {k: v for k, v in profile.counts.items() if k != "_total"}
            if profile else {}
        ),
    }

    playlist_s = sum(e.song.duration_s for e in entries)
    return {
        "summary": {
            "candidate_count": len(songs),
            "skipped_count": len(skipped),
            "predicted_run_s": round(total_s),
            "playlist_s": round(playlist_s),
            "cadence_spm": model.cadence_spm,
            "track_count": len(entries),
        },
        "personalization": personalization,
        "entries": [
            {
                "order": e.order,
                "start_s": round(e.start_s),
                "end_s": round(e.end_s),
                "terrain": e.terrain.value,
                "title": e.song.title,
                "artist": e.song.artist_str,
                "bpm": round(e.song.tempo),
                "energy": round(e.song.energy, 2),
                "reason": e.reason,
                "spotify_id": e.song.spotify_id,
                "url": f"https://open.spotify.com/track/{e.song.spotify_id}",
                "duration_s": round(e.song.duration_s),
            }
            for e in entries
        ],
    }


@app.post("/api/feedback")
async def api_feedback(request: Request):
    """Record a 👍/👎 on a generated song as a labeled training row.

    Body: ``{"spotify_id": str, "label": 0|1}``. Features come from the cache the
    last generate stored for this session, so the browser only sends the verdict.
    """
    body = await request.json()
    spotify_id = (body.get("spotify_id") or "").strip()
    label = body.get("label")
    if not spotify_id or label not in (0, 1, True, False):
        raise HTTPException(400, "Need a spotify_id and a label of 0 or 1.")

    cached = _FEATURES.get(_sid(request), {}).get(spotify_id)
    if not cached:
        raise HTTPException(400, "Unknown track for this session — regenerate first.")

    learn.append_event(cached["features"], cached["terrain"], int(bool(label)),
                       spotify_id=spotify_id)
    return {"ok": True, "n_total": learn.n_ratings()}


@app.post("/api/feedback/reset")
async def api_feedback_reset():
    """Forget all learning — clear the feedback log and cached weights."""
    learn.reset()
    return {"ok": True, "n_total": 0}


@app.post("/api/ytmusic/create")
async def api_ytmusic_create(request: Request):
    """Create a YouTube Music playlist from the terrain-ordered tracks.

    Body: ``{"title": str, "tracks": [{"title","artist","duration_s"}, ...]}``.
    Each track is re-resolved to a YT Music song by search (auto-pick best by
    title/artist overlap + duration anchor), then a private playlist is created
    preserving order. Needs a YT Music auth file (see ``runic.ytmusic``).
    """
    from runic.ytmusic import create_playlist, get_client, match_tracks

    body = await request.json()
    tracks = body.get("tracks") or []
    title = (body.get("title") or "Runic run").strip()
    if not tracks:
        raise HTTPException(400, "No tracks to add.")

    try:
        yt = get_client()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc

    matches = match_tracks(yt, tracks)
    video_ids = [m.video_id for m in matches if m.video_id]
    if not video_ids:
        raise HTTPException(400, "Could not match any tracks on YouTube Music.")

    try:
        playlist_id = create_playlist(yt, title, video_ids)
    except Exception as exc:  # surface YT Music / auth failures to the UI
        raise HTTPException(400, f"YouTube Music playlist creation failed: {exc}") from exc

    return {
        "playlist_id": playlist_id,
        "url": f"https://music.youtube.com/playlist?list={playlist_id}",
        "matched": len(video_ids),
        "total": len(tracks),
        "unmatched": [m.title for m in matches if not m.video_id],
    }


# --- Static frontend ----------------------------------------------------------


@app.get("/")
def index():
    # no-cache so a freshly pulled/edited index.html is always picked up.
    return FileResponse(
        _STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
    )


class _NoCacheStatic(StaticFiles):
    """StaticFiles that asks the browser to revalidate every asset.

    Local self-host tool: each user pulls and runs their own copy, so when the
    CSS/JS changes (a git pull, or hacking locally) a normal refresh must show
    it. Without this, browsers heuristically cache /static assets and serve a
    stale style.css/app.js — which silently breaks new UI (e.g. the map div
    collapsing to zero height because the old CSS lacks its rule). ``no-cache``
    keeps caching (cheap 304s via ETag) but forces revalidation, so assets are
    never stale.
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _NoCacheStatic(directory=_STATIC_DIR), name="static")


def run() -> None:
    """Console-script entry point: ``runic-web``."""
    import uvicorn

    uvicorn.run("runic.web.app:app", host=_HOST, port=_PORT, reload=False)


if __name__ == "__main__":
    run()
