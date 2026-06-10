"""User-authorized Spotify access (Authorization Code flow) for CREATING a
playlist in the user's account.

Reading public playlists needs no auth (see spotify_client). Writing does: it
requires a user login + the playlist-modify scopes. We run a tiny local web
server to catch the OAuth redirect, exchange the code for tokens, and cache the
refresh token so subsequent runs skip the browser step.

Prerequisite: add the redirect URI (default http://127.0.0.1:8888/callback) to
your app at https://developer.spotify.com/dashboard -> Settings -> Redirect URIs.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

from .config import SpotifyCredentials

_AUTH_URL = "https://accounts.spotify.com/authorize"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_API = "https://api.spotify.com/v1"
_SCOPES = "playlist-modify-public playlist-modify-private"
_TOKEN_CACHE = Path(".runic_token.json")
_TIMEOUT = 20


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot handler that captures the ?code= (or ?error=) from the redirect."""

    code: str | None = None
    error: str | None = None
    state: str | None = None

    def do_GET(self):  # noqa: N802 (http.server API)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = (params.get("code") or [None])[0]
        _CallbackHandler.error = (params.get("error") or [None])[0]
        _CallbackHandler.state = (params.get("state") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Runic: authorization complete — you can close this tab."
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *args):  # silence the default stderr logging
        pass


def _basic_auth_header(creds: SpotifyCredentials) -> dict[str, str]:
    raw = f"{creds.client_id}:{creds.client_secret}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _save_tokens(data: dict) -> None:
    data = dict(data)
    data["_acquired_at"] = time.time()
    _TOKEN_CACHE.write_text(json.dumps(data), encoding="utf-8")


def _load_tokens() -> dict | None:
    if _TOKEN_CACHE.exists():
        try:
            return json.loads(_TOKEN_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def _refresh_access_token(creds: SpotifyCredentials, refresh_token: str) -> str | None:
    resp = requests.post(
        _TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        headers=_basic_auth_header(creds),
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        return None
    payload = resp.json()
    # Refresh responses may omit the refresh_token; keep the old one.
    payload.setdefault("refresh_token", refresh_token)
    _save_tokens(payload)
    return payload["access_token"]


def _browser_authorize(creds: SpotifyCredentials, redirect_uri: str, port: int) -> str:
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode(
        {
            "client_id": creds.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": _SCOPES,
            "state": state,
        }
    )
    auth_url = f"{_AUTH_URL}?{query}"

    print("Opening browser to authorize Runic with Spotify...")
    print(f"If it doesn't open, visit:\n{auth_url}")
    webbrowser.open(auth_url)

    _CallbackHandler.code = _CallbackHandler.error = None
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.handle_request()  # blocks until the single redirect arrives
    server.server_close()

    if _CallbackHandler.error:
        raise RuntimeError(f"Spotify authorization denied: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise RuntimeError("No authorization code received from Spotify.")
    if _CallbackHandler.state != state:
        raise RuntimeError("OAuth state mismatch — aborting for safety.")

    resp = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": _CallbackHandler.code,
            "redirect_uri": redirect_uri,
        },
        headers=_basic_auth_header(creds),
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed ({resp.status_code}): {resp.text[:200]}\n"
            f"Is {redirect_uri} registered in your Spotify app's Redirect URIs?"
        )
    payload = resp.json()
    _save_tokens(payload)
    return payload["access_token"]


def get_user_access_token(
    creds: SpotifyCredentials, *, port: int = 8888
) -> str:
    """Return a user access token, reusing a cached refresh token when possible."""
    cached = _load_tokens()
    if cached and cached.get("refresh_token"):
        token = _refresh_access_token(creds, cached["refresh_token"])
        if token:
            return token  # silent refresh succeeded
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    return _browser_authorize(creds, redirect_uri, port)


# --- Playlist write operations ------------------------------------------------


def get_current_user_id(token: str) -> str:
    resp = requests.get(
        f"{_API}/me", headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()["id"]


def create_playlist(
    token: str, user_id: str, name: str, *, public: bool, description: str = ""
) -> tuple[str, str]:
    """Create an empty playlist; return (playlist_id, web_url)."""
    resp = requests.post(
        f"{_API}/users/{user_id}/playlists",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"name": name, "public": public, "description": description},
        timeout=_TIMEOUT,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Playlist creation failed ({resp.status_code}): {resp.text[:200]}"
        )
    data = resp.json()
    return data["id"], data.get("external_urls", {}).get("spotify", "")


def add_tracks(token: str, playlist_id: str, spotify_ids: list[str]) -> None:
    """Add tracks (in order) to a playlist, 100 at a time."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    uris = [f"spotify:track:{sid}" for sid in spotify_ids]
    for i in range(0, len(uris), 100):
        chunk = uris[i : i + 100]
        resp = requests.post(
            f"{_API}/playlists/{playlist_id}/tracks",
            headers=headers,
            json={"uris": chunk},
            timeout=_TIMEOUT,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Adding tracks failed ({resp.status_code}): {resp.text[:200]}"
            )
