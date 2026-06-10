"""Render the ordered playlist to the console and to CSV/JSON files."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .models import PlaylistEntry


def _fmt_mmss(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def print_table(
    entries: list[PlaylistEntry],
    total_run_s: float,
    *,
    skipped: int = 0,
    console: Console | None = None,
) -> None:
    """Pretty-print the playlist as a rich table with a summary footer."""
    console = console or Console()
    table = Table(title="Runic — terrain-matched playlist", header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Time")
    table.add_column("Terrain")
    table.add_column("Song")
    table.add_column("Artist")
    table.add_column("BPM", justify="right")
    table.add_column("Energy", justify="right")
    table.add_column("Why")

    for e in entries:
        table.add_row(
            str(e.order),
            f"{_fmt_mmss(e.start_s)}–{_fmt_mmss(e.end_s)}",
            e.terrain.value,
            e.song.title,
            e.song.artist_str,
            f"{e.song.tempo:.0f}",
            f"{e.song.energy:.2f}",
            e.reason,
        )

    console.print(table)
    playlist_s = entries[-1].end_s if entries else 0.0
    console.print(
        f"[bold]{len(entries)} songs[/bold] · playlist {_fmt_mmss(playlist_s)} "
        f"vs predicted run {_fmt_mmss(total_run_s)}"
        + (f" · {skipped} track(s) skipped (not in ReccoBeats)" if skipped else "")
    )


def write_csv(entries: list[PlaylistEntry], path: str | Path) -> None:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["order", "start_s", "end_s", "terrain", "title", "artists",
             "tempo", "energy", "valence", "danceability", "spotify_id", "reason"]
        )
        for e in entries:
            writer.writerow(
                [e.order, round(e.start_s, 1), round(e.end_s, 1), e.terrain.value,
                 e.song.title, e.song.artist_str, e.song.tempo, e.song.energy,
                 e.song.valence, e.song.danceability, e.song.spotify_id, e.reason]
            )


def write_json(entries: list[PlaylistEntry], path: str | Path) -> None:
    path = Path(path)
    payload = [
        {
            "order": e.order,
            "start_s": round(e.start_s, 1),
            "end_s": round(e.end_s, 1),
            "terrain": e.terrain.value,
            "title": e.song.title,
            "artists": list(e.song.artists),
            "spotify_id": e.song.spotify_id,
            "tempo": e.song.tempo,
            "energy": e.song.energy,
            "valence": e.song.valence,
            "danceability": e.song.danceability,
            "reason": e.reason,
        }
        for e in entries
    ]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_output(entries: list[PlaylistEntry], path: str | Path) -> None:
    """Dispatch to CSV or JSON based on file extension."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        write_json(entries, path)
    else:
        write_csv(entries, path)
