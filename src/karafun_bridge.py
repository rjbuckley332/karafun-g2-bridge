"""Karafun (macOS) → OnSong-shaped lyric bridge.

Watches Karafun's macOS container cache (fsCachedData) for song metadata XML
and timed-karaoke XML, extracts the time-synced lyrics, and serves an
OnSong-compatible /api/live JSON endpoint so your existing G2Bridge can drive
the Even G2 teleprompter unchanged.

Karafun on Mac does NOT use .kfn files. It caches:
  1. Song metadata XML:  <song id="11291" content="2d53ee"><title>...</title><artist>...</artist>...</song>
  2. Karaoke XML:        <karaoke><page><line><word><syllabe><start>00:00:00,750</start><end>...</end><text>...</text></syllabe></word></line></page>

G2Bridge stays untouched — it polls /api/live exactly like OnSong.

Zero external dependencies — pure Python 3 stdlib.

Run:
    python3 src/karafun_bridge.py --config config.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

__version__ = "0.3.0"

# --- Timestamp parsing: HH:MM:SS,mmm ---

_TIMESTAMP_RE = re.compile(r"^(\d+):(\d+):(-?\d+),(\d+)$")


def parse_timestamp(ts: str) -> int:
    """Parse 'HH:MM:SS,mmm' (SS may be negative for pre-roll) into ms."""
    m = _TIMESTAMP_RE.match(ts.strip())
    if not m:
        return 0
    h, mi, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return ((h * 60 + mi) * 60 + s) * 1000 + ms


# --- Karaoke XML parsing ---

def _strip_xml_prefix(text: str) -> str:
    """Remove binary junk before <?xml and after the root closing tag.

    Karafun's cached karaoke files embed binary image data (JPEG/EXIF)
    before/after the XML document. We keep only the XML portion.
    """
    start = text.find("<?xml")
    if start > 0:
        text = text[start:]
    # Truncate at the first closing root tag so embedded binary is dropped.
    for end_tag in ("</root>", "</karaoke>", "</response>"):
        idx = text.find(end_tag)
        if idx >= 0:
            text = text[: idx + len(end_tag)]
            break
    return text


def parse_karaoke_xml(xml_text: str) -> list[dict]:
    """Parse Karafun karaoke XML into a list of {ms, text} lyric line events.

    Each line's start time is the earliest syllable start within that line.
    """
    events: list[dict] = []
    xml_text = _strip_xml_prefix(xml_text)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return events

    for page in root.iter("page"):
        for line in page.iter("line"):
            line_text_parts: list[str] = []
            line_start_ms: int | None = None

            for word in line.iter("word"):
                word_text = ""
                word_start_ms: int | None = None
                for sy in word.iter("syllabe"):
                    text_el = sy.find("text")
                    if text_el is not None and text_el.text:
                        word_text += text_el.text
                    start_el = sy.find("start")
                    if start_el is not None and start_el.text:
                        ms = parse_timestamp(start_el.text)
                        if word_start_ms is None or ms < word_start_ms:
                            word_start_ms = ms
                if word_text:
                    line_text_parts.append(word_text)
                if word_start_ms is not None:
                    if line_start_ms is None or word_start_ms < line_start_ms:
                        line_start_ms = word_start_ms

            if line_text_parts and line_start_ms is not None:
                events.append({
                    "ms": line_start_ms,
                    "text": " ".join(line_text_parts),
                })

    events.sort(key=lambda e: e["ms"])
    return events


def parse_song_meta_xml(xml_text: str) -> dict | None:
    """Parse song metadata XML into {title, artist, year, song_id}."""
    xml_text = _strip_xml_prefix(xml_text)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    song_el = root.find("song")
    if song_el is None:
        return None

    title_el = song_el.find("title")
    artist_el = song_el.find("artist")
    year_el = song_el.find("year")

    return {
        "song_id": song_el.get("id"),
        "title": title_el.text if title_el is not None else "",
        "artist": artist_el.text if artist_el is not None else "",
        "year": year_el.text if year_el is not None else "",
    }


# --- OnSong-shaped payload builder ---

LINES_PER_SCREEN = 4  # number of lyric lines to show at once


def build_live_payload(song_title: str, key: str, section: int,
                       section_name: str, timeline: list[dict],
                       now_ms: int, fallback_line: str = "") -> dict:
    """Build the OnSong-shaped /api/live payload G2Bridge consumes.

    Returns the current line plus the next (LINES_PER_SCREEN - 1) upcoming
    lines, so the teleprompter can show several lines at once.
    """
    # Find the index of the current line (the last line whose start <= now_ms).
    current_idx = -1
    for i, ev in enumerate(timeline):
        if ev["ms"] <= now_ms:
            current_idx = i
        else:
            break

    if current_idx < 0:
        # Song hasn't started yet; show fallback as the first line.
        lines = [{"parts": [fallback_line]}] if fallback_line else []
    else:
        end = min(current_idx + LINES_PER_SCREEN, len(timeline))
        lines = [{"parts": [ev["text"]]} for ev in timeline[current_idx:end]]

    return {
        "songTitle": song_title,
        "key": key,
        "section": section,
        "sectionName": section_name,
        "lines": lines,
    }


# --- Bridge state ---

class BridgeState:
    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.Lock()
        self.timeline: list[dict] = []
        self.song_start_ms: float | None = None
        self.current_song: dict | None = None
        self.payload: dict = build_live_payload(
            song_title=config.get("song_title", "Karafun Song"),
            key=config.get("key", "C"),
            section=1,
            section_name="",
            timeline=[],
            now_ms=0,
            fallback_line=config.get("fallback_line", ""),
        )
        # Pairing: metadata XML -> karaoke XML by song id
        self._meta_by_id: dict[str, dict] = {}      # song_id -> meta
        self._karaoke_by_id: dict[str, str] = {}     # song_id -> xml text
        self._latest_meta: dict | None = None

    def load_meta(self, xml_text: str) -> dict | None:
        meta = parse_song_meta_xml(xml_text)
        if not meta:
            return None
        with self.lock:
            song_id = meta.get("song_id")
            if song_id:
                self._meta_by_id[song_id] = meta
            self._latest_meta = meta
            # If we already have karaoke for this song, build the timeline now
            if song_id and song_id in self._karaoke_by_id:
                self._activate_song(meta, self._karaoke_by_id[song_id])
        return meta

    def load_karaoke(self, xml_text: str) -> None:
        """Store karaoke XML; if we have matching meta, activate the song."""
        # The karaoke XML doesn't carry the song id, so we pair by recency:
        # the latest meta + this karaoke.
        with self.lock:
            if self._latest_meta and self._latest_meta.get("song_id"):
                self._karaoke_by_id[self._latest_meta["song_id"]] = xml_text
                self._activate_song(self._latest_meta, xml_text)

    def _activate_song(self, meta: dict, karaoke_text: str) -> None:
        timeline = parse_karaoke_xml(karaoke_text)
        now = time.time() * 1000
        self.timeline = timeline
        self.song_start_ms = now
        self.current_song = meta
        title = meta.get("title") or self.config.get("song_title", "Karafun Song")
        artist = meta.get("artist", "")
        display_title = f"{title} — {artist}" if artist else title
        self.payload = build_live_payload(
            song_title=display_title,
            key=self.config.get("key", "C"),
            section=1,
            section_name="",
            timeline=timeline,
            now_ms=0,  # song just started
            fallback_line=self.config.get("fallback_line", ""),
        )
        print(f"[bridge] activated song: {display_title} ({len(timeline)} lyric lines)")

    def get_payload(self) -> dict:
        with self.lock:
            if self.song_start_ms is not None and self.timeline:
                now_ms = int((time.time() * 1000) - self.song_start_ms)
                title = self.config.get("song_title", "Karafun Song")
                if self.current_song:
                    t = self.current_song.get("title") or title
                    a = self.current_song.get("artist", "")
                    title = f"{t} — {a}" if a else t
                return build_live_payload(
                    song_title=title,
                    key=self.config.get("key", "C"),
                    section=1,
                    section_name="",
                    timeline=self.timeline,
                    now_ms=now_ms,
                    fallback_line=self.config.get("fallback_line", ""),
                )
            return dict(self.payload)


# --- File watcher (polling, zero deps) ---

SONG_META_PREFIX = b'<?xml version="1.0" encoding="UTF-8"?>\n<response status="OK">\n<song'
KARAOKE_PREFIX = b'<karaoke'


def classify_file(path: Path) -> str | None:
    """Return 'meta', 'karaoke', or None based on file content.

    Some cached files start with binary junk before the XML declaration, so we
    read a larger chunk and search for the markers anywhere in it.
    """
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
    except OSError:
        return None
    if SONG_META_PREFIX in chunk or b"<song" in chunk:
        return "meta"
    if KARAOKE_PREFIX in chunk or b"<karaoke" in chunk:
        return "karaoke"
    return None


class PollWatcher(threading.Thread):
    def __init__(self, cache_dir: str, state: BridgeState, interval: float = 1.0):
        super().__init__(daemon=True)
        self.cache_dir = Path(cache_dir)
        self.state = state
        self.interval = interval
        self._stop = threading.Event()
        self._seen: dict[str, float] = {}  # path -> mtime

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        print(f"[bridge] watching {self.cache_dir}")
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception as exc:  # noqa: BLE001
                print(f"[bridge] watcher error: {exc}")
            self._stop.wait(self.interval)

    def _scan(self) -> None:
        if not self.cache_dir.exists():
            return
        for p in self.cache_dir.iterdir():
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            path = str(p)
            prev_mtime = self._seen.get(path)
            if prev_mtime is not None and st.st_mtime == prev_mtime:
                continue
            self._seen[path] = st.st_mtime
            kind = classify_file(p)
            if kind == "meta":
                try:
                    with open(p, "r", errors="replace") as f:
                        self.state.load_meta(f.read())
                except Exception as exc:  # noqa: BLE001
                    print(f"[bridge] error reading meta {p.name}: {exc}")
            elif kind == "karaoke":
                try:
                    with open(p, "r", errors="replace") as f:
                        self.state.load_karaoke(f.read())
                except Exception as exc:  # noqa: BLE001
                    print(f"[bridge] error reading karaoke {p.name}: {exc}")


# --- HTTP server ---

class LiveHandler(BaseHTTPRequestHandler):
    state: BridgeState  # set by the server factory

    def do_GET(self):  # noqa: N802
        # Serve OnSong-compatible JSON at root (/) so G2Chords / G2Bridge
        # can connect with just the IP address (no path, no port in UI).
        # /api/live and /healthz are kept for compatibility.
        if self.path in ("/", "/api/live"):
            payload = self.state.get_payload()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/healthz":
            body = b'{"status":"ok"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):  # silence default noise
        return


def load_config(config_path: str | None) -> dict:
    config = {
        "cache_dir": os.environ.get("KARAFUN_CACHE_DIR", ""),
        "host": os.environ.get("KARAFUN_HOST", "0.0.0.0"),
        "port": int(os.environ.get("KARAFUN_PORT", "8766")),
        "poll_interval_sec": float(os.environ.get("KARAFUN_POLL", "1.0")),
        "song_title": os.environ.get("KARAFUN_SONG_TITLE", "Karafun Song"),
        "key": os.environ.get("KARAFUN_KEY", "C"),
        "fallback_line": os.environ.get("KARAFUN_FALLBACK_LINE", ""),
    }
    if config_path and Path(config_path).exists():
        with open(config_path) as f:
            file_cfg = json.load(f)
        config.update(file_cfg)
    return config


def seed_existing(state: BridgeState, cache_dir: str) -> None:
    """Seed from any existing meta + karaoke XML files so we're not empty on start."""
    cache_path = Path(cache_dir)
    if not cache_path.exists():
        return
    meta_files = []
    karaoke_files = []
    for f in cache_path.iterdir():
        if not f.is_file():
            continue
        kind = classify_file(f)
        if kind == "meta":
            meta_files.append(f)
        elif kind == "karaoke":
            karaoke_files.append(f)

    # Load all metas
    for f in meta_files:
        try:
            with open(f, "r", errors="replace") as fh:
                state.load_meta(fh.read())
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] seed skip meta {f.name}: {exc}")

    # Load all karaoke (pairs with latest meta)
    for f in karaoke_files:
        try:
            with open(f, "r", errors="replace") as fh:
                state.load_karaoke(fh.read())
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] seed skip karaoke {f.name}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Karafun (macOS) → G2Bridge OnSong-shaped lyric bridge")
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--cache-dir", help="override cache directory")
    parser.add_argument("--port", type=int, default=8766, help="HTTP port")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.cache_dir:
        config["cache_dir"] = args.cache_dir
    if args.port:
        config["port"] = args.port

    cache_dir = config.get("cache_dir", "")
    if not cache_dir or not Path(cache_dir).exists():
        print(f"[bridge] cache_dir not found: {cache_dir}")
        print(f"       Expected: ~/Library/Containers/com.recisio.kfiphone/Data/Library/Caches/com.recisio.kfiphone/fsCachedData")
        print("       Run: python3 src/discover_cache.py  (after playing a song in Karafun)")
        return 1

    state = BridgeState(config)
    LiveHandler.state = state

    # Seed from existing files.
    seed_existing(state, cache_dir)
    if state.current_song:
        print(f"[bridge] seeded from existing cache: {state.current_song.get('title')}")

    # Start the polling file watcher.
    watcher = PollWatcher(cache_dir, state, interval=config.get("poll_interval_sec", 1.0))
    watcher.start()

    # Start HTTP server.
    server = ThreadingHTTPServer((config["host"], config["port"]), LiveHandler)
    print(f"[bridge] HTTP on http://{config['host']}:{config['port']}/api/live")
    print("[bridge] point G2Bridge at that URL. Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] shutting down...")
    finally:
        watcher.stop()
        watcher.join(timeout=2)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
