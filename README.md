# Runic

Terrain-aware running playlist generator. Give it a **GPX route** and your
**pace**, and it builds an **ordered playlist** whose songs match the terrain:
energetic, on-cadence tracks for the descents (attack the downhills), chiller
cruise tracks for the climbs (recover on the ups), with the total length fitted
to your predicted run time.

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
- **Energy** is scaled to grade: fast descents want ~0.9, climbs cruise around ~0.5.
- **Minetti** grade cost makes climbs take longer, so songs land on the right
  part of the route in *time*.

---

## Setup

Runic is **not hosted** — you run it on your own machine. It's a small FastAPI
app: upload a GPX, log into Spotify, pick playlists, and it builds the
terrain-matched list with an in-browser player. Everything (including any auth
tokens) stays local.

Do these three steps **before** running anything. Step 3 is optional.

### Step 1 — Create a Spotify app (required, ~2 min)

There are no shared credentials — `.env` is git-ignored and never committed, so
**everyone runs their own Spotify app**:

1. Go to <https://developer.spotify.com/dashboard> → **Create app**.
2. Add this **Redirect URI** exactly: `http://127.0.0.1:8888/callback`, then **Save**.
3. Open the app's **Settings** and copy the **Client ID** and **Client Secret** —
   you'll paste them into `.env` in Step 2.
4. **Dev-mode caveats** (Spotify locks new apps to development mode):
   - Under **Settings → User Management**, add yourself to the allowlist — enter the
     **display name *and* email** of the Spotify account you'll log in with.
     Dev-mode apps reject any account that isn't on this list.
   - The **in-browser player needs Spotify Premium** (Web Playback SDK requirement),
     and the player is **desktop-browser only** — it will *not* play inside iOS
     Safari or Android Chrome. (Mobile playback is covered by the exports below.)

### Step 2 — Clone, install, and add your credentials

```bash
git clone https://github.com/Sai-Danush/RuNiC.git && cd RuNiC
python -m venv .venv && source .venv/bin/activate
pip install -e ".[web]"     # FastAPI + uvicorn + ytmusicapi, plus the engine

cp .env.example .env        # then paste the Client ID + Secret from Step 1 (git-ignored)
```

### Step 3 — Connect YouTube Music (optional)

Only needed for the **"Create YT Music playlist"** export (native playback on your
phone). Skip it if you'll use the Spotify/CSV route instead.

`ytmusicapi` needs your YT Music session, read from a local `browser.json`
(git-ignored, never leaves your machine):

1. Open <https://music.youtube.com> **logged in**, open DevTools → **Network**,
   and type `youtubei` in the filter box.
2. Click **any** request to `music.youtube.com/youtubei/v1/...` — `browse` is the
   usual one, but if you don't see it, `next`, `account/account_menu`, or any other
   `youtubei/v1/` call works just as well (they all carry the same login cookie).
   If the list is empty, scroll the page or click around to trigger one. Then
   right-click the request → **Copy → Copy as cURL**.
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

---

## Running it

```bash
runic-web                    # serves http://127.0.0.1:8888
```

Open <http://127.0.0.1:8888>, **Log in with Spotify**, upload your GPX, add a
personal best (e.g. `5k=22:30`), pick one or more **public** playlists as the song
pool, and hit **Generate**. Play it right there in the browser, or export it to
your phone (below).

## Get the playlist onto your phone

Both buttons are in the results panel:

- **→ Spotify (any device): "Download CSV"** → import the CSV at
  [tunemymusic.com](https://www.tunemymusic.com) (free, ≤500 tracks/transfer) to
  create a real Spotify playlist that plays natively on your phone. (Runic can't
  create the Spotify playlist directly — dev-mode apps get a 403 on playlist
  creation — hence the CSV → TuneMyMusic hop.)
- **→ YouTube Music (native mobile playback): "Create YT Music playlist"** →
  creates a real private YT Music playlist on your account. Requires the one-time
  auth setup from **Step 3** above.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

## Limitations

- **Spotify playlists are public-only.** Runic reads the track list of public
  playlists as the song pool; make a private one public temporarily to use it.
- **Runic can't create a Spotify playlist directly** — dev-mode apps get a 403 on
  playlist creation, so getting the result into Spotify goes through the CSV →
  [TuneMyMusic](https://www.tunemymusic.com) hop. (YT Music playlists *are* created
  natively.)
- **The in-browser player needs Spotify Premium and a desktop browser** — it won't
  play on iOS Safari or Android Chrome. Use the exports for mobile playback.
