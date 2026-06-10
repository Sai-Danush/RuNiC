"""Configuration loading (Spotify credentials from environment / .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class SpotifyCredentials:
    client_id: str
    client_secret: str


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def load_spotify_credentials() -> SpotifyCredentials:
    """Load Spotify client credentials from the environment.

    Reads a local ``.env`` file if present, then falls back to real environment
    variables. Raises :class:`ConfigError` with an actionable message if either
    value is missing.
    """
    load_dotenv()  # no-op if .env is absent; real env vars still work
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()

    missing = [
        name
        for name, value in (
            ("SPOTIFY_CLIENT_ID", client_id),
            ("SPOTIFY_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "Missing Spotify credentials: "
            + ", ".join(missing)
            + ".\nCopy .env.example to .env and fill in your values "
            "(https://developer.spotify.com/dashboard), or use --candidates-json "
            "for offline mode (no Spotify needed)."
        )
    return SpotifyCredentials(client_id=client_id, client_secret=client_secret)
