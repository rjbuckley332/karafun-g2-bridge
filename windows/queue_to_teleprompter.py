"""Karafun Queue → G2 Teleprompter lyrics mapper.

Reads the Karafun queue from user_database.db, matches each song to a
predefined lyrics file in a lyrics folder, and outputs per-song text files
ready to load into the G2 Teleprompter app.

Usage:
    python queue_to_teleprompter.py --config config.json

config.json:
{
  "appdata_dir": "C:/Users/susqu/AppData/Roaming",
  "lyrics_dir": "C:/Users/susqu/Documents/KaraFunLyrics",
  "output_dir": "C:/Users/susqu/Documents/KaraFunTeleprompter",
  "song_title_pattern": "{artist} - {title}"
}
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

__version__ = "1.0.0"


def query_queue(appdata_dir: str) -> list[dict]:
    """Query Karafun's user_database.db for the current queue.

    Returns a list of dicts: {item_id, singer, title, artist, bpm}
    ordered by queue position (item_id ASC).
    """
    source_db = os.path.join(appdata_dir, "KaraFun", "user_database.db")
    source_wal = source_db + "-wal"

    if not os.path.exists(source_db):
        print(f"[queue] database not found: {source_db}")
        return []

    temp_dir = tempfile.mkdtemp()
    temp_db = os.path.join(temp_dir, "snapshot.db")
    temp_wal = os.path.join(temp_dir, "snapshot.db-wal")

    try:
        shutil.copy2(source_db, temp_db)
        if os.path.exists(source_wal):
            shutil.copy2(source_wal, temp_wal)

        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()

        query = """
        SELECT 
            q.item_id,
            q.singer_name,
            s.title,
            s.artist,
            s.bpm
        FROM queue_item q
        JOIN song s ON q.item_ref_id = s.song_id
        ORDER BY q.item_id ASC;
        """

        cursor.execute(query)
        rows = cursor.fetchall()
        conn.close()

        results = []
        for item_id, singer, title, artist, bpm in rows:
            results.append({
                "item_id": item_id,
                "singer": singer,
                "title": title,
                "artist": artist,
                "bpm": bpm,
            })

        return results
    except Exception as exc:  # noqa: BLE001
        print(f"[queue] error querying queue: {exc}")
        return []
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename."""
    keep = "".join(c for c in name if c.isalnum() or c in " -_.")
    return keep.strip().replace(" ", "_")


def find_lyrics_file(lyrics_dir: Path, song: dict) -> Path | None:
    """Find a lyrics file for a song in the lyrics directory.

    Looks for files matching:
      - <Artist> - <Title>.txt
      - <Title>.txt
      - <Artist>_<Title>.txt
    """
    title = song.get("title", "")
    artist = song.get("artist", "")

    candidates = [
        f"{artist} - {title}.txt",
        f"{title}.txt",
        f"{artist}_{title}.txt",
        f"{artist} - {title}.lrc",
        f"{title}.lrc",
        f"{artist}_{title}.lrc",
    ]

    for name in candidates:
        p = lyrics_dir / name
        if p.exists():
            return p

    # Also try case-insensitive match
    for p in lyrics_dir.iterdir():
        if not p.is_file():
            continue
        stem = p.stem.lower()
        title_lower = title.lower()
        artist_lower = artist.lower()
        if stem == title_lower or stem == f"{artist_lower} - {title_lower}" or stem == f"{artist_lower}_{title_lower}":
            return p

    return None


def read_lyrics(path: Path) -> list[str]:
    """Read a lyrics file and return a list of lines."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n\r") for line in f]
        # Remove empty lines at start/end but keep internal structure
        return [l for l in lines if l.strip() != ""]
    except Exception as exc:  # noqa: BLE001
        print(f"[lyrics] error reading {path}: {exc}")
        return []


def write_teleprompter_script(output_dir: Path, song: dict, lines: list[str]) -> Path | None:
    """Write a single-song teleprompter script file."""
    safe_title = sanitize_filename(song.get("title", "unknown"))
    safe_artist = sanitize_filename(song.get("artist", "unknown"))
    out_name = f"{safe_artist} - {safe_title}.txt"
    out_path = output_dir / out_name

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"# {song.get('title', '')} — {song.get('artist', '')}\n")
            f.write(f"# Queue position: {song.get('item_id', '?')}\n")
            f.write(f"# BPM: {song.get('bpm', '?')}\n")
            f.write("#\n")
            for line in lines:
                f.write(line + "\n")
        return out_path
    except Exception as exc:  # noqa: BLE001
        print(f"[teleprompter] error writing {out_path}: {exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Karafun Queue → G2 Teleprompter lyrics mapper"
    )
    parser.add_argument("--config", default="config.json", help="path to config.json")
    args = parser.parse_args()

    config: dict[str, Any] = {}
    config_path = Path(args.config)
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)

    appdata_dir = config.get("appdata_dir", "")
    lyrics_dir_str = config.get("lyrics_dir", "")
    output_dir_str = config.get("output_dir", "")

    if not appdata_dir:
        print("[error] appdata_dir not set in config.json")
        return 1
    if not lyrics_dir_str:
        print("[error] lyrics_dir not set in config.json")
        return 1

    appdata_path = Path(appdata_dir)
    lyrics_dir = Path(lyrics_dir_str)
    output_dir = Path(output_dir_str) if output_dir_str else lyrics_dir / "teleprompter"

    if not appdata_path.exists():
        print(f"[error] appdata_dir not found: {appdata_dir}")
        return 1
    if not lyrics_dir.exists():
        print(f"[error] lyrics_dir not found: {lyrics_dir_str}")
        print("    Create this folder and add .txt lyrics files named '<Artist> - <Title>.txt'")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    # Query the queue
    queue = query_queue(appdata_dir)
    if not queue:
        print("[queue] Queue is empty or database not found.")
        return 0

    print(f"[queue] Found {len(queue)} song(s) in queue:")
    for song in queue:
        print(f"  {song['item_id']}. \"{song['title']}\" by {song['artist']}")

    print(f"\n[lyrics] Looking for lyrics files in: {lyrics_dir}")
    print(f"[output] Writing teleprompter scripts to: {output_dir}\n")

    matched = 0
    missing = 0

    for song in queue:
        lyrics_file = find_lyrics_file(lyrics_dir, song)
        if lyrics_file is None:
            print(f"  [MISSING] \"{song['title']}\" by {song['artist']} — no lyrics file found")
            missing += 1
            continue

        lines = read_lyrics(lyrics_file)
        if not lines:
            print(f"  [EMPTY] \"{song['title']}\" by {song['artist']} — no readable lines")
            missing += 1
            continue

        out_path = write_teleprompter_script(output_dir, song, lines)
        if out_path:
            print(f"  [OK] \"{song['title']}\" by {song['artist']} → {out_path.name} ({len(lines)} lines)")
            matched += 1
        else:
            missing += 1

    print(f"\n[summary] Matched: {matched}, Missing: {missing}, Total: {len(queue)}")
    print(f"[output] Teleprompter scripts written to: {output_dir}")
    print("\n[next] Load each .txt file into the G2 Teleprompter app on your Even glasses.")
    print("       One script per song — load the next song between numbers.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
