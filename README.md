# Runic

Terrain-aware running playlist generator. Give it a **GPX route** and your
**pace**, and it builds an **ordered playlist** whose songs match the terrain:
on-cadence, high-energy tracks for the climbs, recovery tracks for the descents,
with the total length fitted to your predicted run time.

Audio features come from [ReccoBeats](https://reccobeats.com) (no API key).
Spotify is used only to read the track list of **public** playlists.

## How it works

```
GPX route ─► terrain segments (grade) ─► effort timeline (time + target energy/tempo)
playlist  ─► Spotify track ids ─► ReccoBeats features (tempo/energy/...) ─► candidates
                                   └─► hybrid match (tempo + energy) ─► ordered list
```

- **Tempo** is matched to your running cadence (octave-aware: a 170 and an 85 BPM
  song both fit a 170 cadence).
- **Energy** is scaled to grade: steep climbs want ~0.9, descents ~0.5.
- **Minetti** grade cost makes climbs take longer, so songs land on the right
  part of the route in *time*.

## Run it yourself — the web app (local)

Runic is **not hosted** — you run it on your own machine. It's a small FastAPI app:
upload a GPX, log into Spotify, pick playlists, and it builds the terrain-matched
list with an in-browser player. Everything (including any auth tokens) stays local.

### 1. Clone + install

```bash
git clone https://github.com/Sai-Danush/RuNiC.git && cd RuNiC
python -m venv .venv && source .venv/bin/activate
pip install -e ".[web]"     # FastAPI + uvicorn + ytmusicapi, plus the engine
```

### 2. Create a Spotify app (required)

1. Go to <https://developer.spotify.com/dashboard> → **Create app**.
2. Add this **Redirect URI** exactly: `http://127.0.0.1:8888/callback`.
3. Copy the **Client ID** and **Client Secret**:
   ```bash
   cp .env.example .env       # then paste both values into .env (git-ignored)
   ```
4. **Dev-mode caveats** (Spotify locks new apps to development mode):
   - Under the app's **User Management**, add the Spotify account you'll log in
     with — dev-mode apps only allow a small allowlist of users.
   - The **in-browser player needs Spotify Premium** (Web Playback SDK requirement),
     and the player is **desktop-browser only** — it will *not* play inside iOS
     Safari or Android Chrome. (Mobile playback is covered by the exports below.)

### 3. Run

```bash
runic-web                    # serves http://127.0.0.1:8888
```

Open <http://127.0.0.1:8888>, **Log in with Spotify**, upload your GPX, add a
personal best (e.g. `5k=22:30`), pick one or more **public** playlists as the song
pool, and hit **Generate**. Play it right there in the browser, or export it:

### 4. Get the playlist onto your phone

Both buttons are in the results panel:

- **→ Spotify (any device): "Download CSV"** → import the CSV at
  [tunemymusic.com](https://www.tunemymusic.com) (free, ≤500 tracks/transfer) to
  create a real Spotify playlist that plays natively on your phone. (Runic can't
  create the Spotify playlist directly — dev-mode apps get a 403 on playlist
  creation — hence the CSV → TuneMyMusic hop.)
- **→ YouTube Music (native mobile playback): "Create YT Music playlist"** →
  creates a real private YT Music playlist on your account. Needs a one-time
  auth-file setup (below).

### 5. One-time YouTube Music setup (only for the YT Music button)

`ytmusicapi` needs your YT Music session. It's read from a local `browser.json`
(git-ignored, never leaves your machine):

1. Open <https://music.youtube.com> **logged in**, open DevTools → **Network**.
2. Click any request to `youtubei/v1/browse`, then right-click it →
   **Copy → Copy as cURL**.
3. In your terminal (macOS):
   ```bash
   pbpaste > /tmp/yt_curl.txt
   python scripts/ytmusic_from_curl.py /tmp/yt_curl.txt   # writes browser.json
   ```
   (Linux: paste into `/tmp/yt_curl.txt` with your editor instead of `pbpaste`.)

The script prints only how many headers it found and whether a cookie is present —
never the values. Safari hides the cookie under curl's `-b` flag, Chrome/Firefox
use `-H 'cookie:'`; both are handled. The cookie eventually expires (usually
months; sooner if you log out or change your password) — just re-run these steps.

> Optional, more durable alternative: `ytmusicapi oauth` sets up a refresh-token
> `oauth.json` instead of a cookie, but needs a Google Cloud OAuth client. Overkill
> for local use — the cookie is simpler.

## Install (CLI only)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .            # adds the `runic` command
# or, without installing:  python -m runic.cli ...
```

## Setup (only needed for live Spotify playlists)

1. Create a free app at <https://developer.spotify.com/dashboard>.
2. `cp .env.example .env` and paste your **Client ID** and **Client Secret**.

Only **public** playlists can be read this way. For a private playlist, make it
public temporarily, or use offline mode (below).

## Usage

```bash
runic --gpx morning_route.gpx \
      --pb 5k=22:30 \
      --playlist https://open.spotify.com/playlist/<id> \
      --out playlist.csv
```

The GPX path can be anywhere — absolute or relative to your terminal.

### Common options

| Flag | Purpose |
|------|---------|
| `--pb 10k=47:00` | Personal best (repeatable); predicts pace via Riegel. |
| `--past-run prev.gpx` | Measure pace/cadence from a past run (repeatable). |
| `--cadence 175` | Override target cadence (steps/min). |
| `--playlist <url>` | Public Spotify playlist as a song pool (repeatable). |
| `--candidates-json pool.json` | Offline song pool — no Spotify/ReccoBeats. |
| `--save-candidates pool.json` | Cache the fetched pool for reuse. |
| `--w-tempo 0.5 --w-energy 0.4` | Beat-sync vs effort-matching weights. |
| `--out playlist.csv` | Write CSV (or `.json`). |

### Offline mode (no credentials)

```bash
runic --gpx tests/fixtures/sample_route.gpx --pb 5k=22:30 \
      --candidates-json tests/fixtures/candidates.json
```

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

## Limitations (v1)

- Reads **public** playlists only (Client-Credentials). Private-playlist support
  and auto-creating the playlist in your account are future work.
- Cadence is estimated when past-run data lacks it; tune with `--cadence`.
