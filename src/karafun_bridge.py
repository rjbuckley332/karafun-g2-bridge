"""Karafun (macOS) → OnSong-shaped lyric bridge.

Watches Karafun's macOS container cache (fsCachedData) for timed-karaoke XML
files, extracts the time-synced lyrics, and serves an OnSong-compatible
/api/live JSON endpoint so G2Bridge / G2Karafun can drive the Even G2
teleprompter unchanged.

Karafun on Mac does NOT use .kfn files. It caches karaoke XML like:
  <karaoke version="1.0" ... duration="250"><kfntracks>...</kfntracks>
    <page id="1"><line id="1"><word id="1"><syllabe><start>00:00:00,750</start><end>00:00:01,360</end><text>Just</text></syllabe></word>...</line></page>
    ...

The bridge watches for new/changed karaoke XML files, parses the timed lyrics,
and serves 4 lines at once (current + 3 upcoming) based on elapsed song time.
Fully automatic — no R1 ring required.

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

__version__ = "0.4.0"

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
    """Remove binary junk before <?xml and after the root closing tag."""
    start = text.find("<?xml")
    if start > 0:
        text = text[start:]
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


# --- OnSong-shaped payload builder ---

LINES_PER_SCREEN = 4  # number of lyric lines to show at once


def build_live_payload(song_title: str, key: str, section: int,
                       section_name: str, timeline: list[dict],
                       now_ms: int, fallback_line: str = "") -> dict:
    """Build the OnSong-shaped /api/live payload.

    Returns the current line plus the next (LINES_PER_SCREEN - 1) upcoming
    lines, so the teleprompter can show several lines at once.
    """
    current_idx = -1
    for i, ev in enumerate(timeline):
        if ev["ms"] <= now_ms:
            current_idx = i
        else:
            break

    if current_idx < 0:
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
        self.current_song_title: str = config.get("song_title", "Karafun Song")
        self.payload: dict = build_live_payload(
            song_title=self.current_song_title,
            key=config.get("key", "C"),
            section=1,
            section_name="",
            timeline=[],
            now_ms=0,
            fallback_line=config.get("fallback_line", ""),
        )
        # Track the most recently seen karaoke file path to avoid re-activating.
        self._last_karaoke_path: str | None = None

    def activate_from_karaoke(self, xml_text: str, song_title: str) -> None:
        """Parse a karaoke XML file and activate it as the current song."""
        timeline = parse_karaoke_xml(xml_text)
        if not timeline:
            print("[bridge] karaoke XML had no parseable lyric lines; skipping")
            return
        now = time.time() * 1000
        self.timeline = timeline
        self.song_start_ms = now
        self.current_song_title = song_title
        self.payload = build_live_payload(
            song_title=song_title,
            key=self.config.get("key", "C"),
            section=1,
            section_name="",
            timeline=timeline,
            now_ms=0,  # song just started
            fallback_line=self.config.get("fallback_line", ""),
        )
        print(f"[bridge] activated song: {song_title} ({len(timeline)} lyric lines)")

    def get_payload(self) -> dict:
        with self.lock:
            if self.song_start_ms is not None and self.timeline:
                now_ms = int((time.time() * 1000) - self.song_start_ms)
                return build_live_payload(
                    song_title=self.current_song_title,
                    key=self.config.get("key", "C"),
                    section=1,
                    section_name="",
                    timeline=self.timeline,
                    now_ms=now_ms,
                    fallback_line=self.config.get("fallback_line", ""),
                )
            return dict(self.payload)


# --- File watcher (polling, zero deps) ---

KARAOKE_PREFIX = b'<karaoke'


def classify_karaoke(path: Path) -> bool:
    """Return True if the file contains a <karaoke> element."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
    except OSError:
        return False
    return b"<karaoke" in chunk


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
            if classify_karaoke(p):
                try:
                    with open(p, "r", errors="replace") as f:
                        xml_text = f.read()
                    # Use filename (minus extension) as the song title.
                    song_title = p.stem
                    self.state.activate_from_karaoke(xml_text, song_title)
                except Exception as exc:  # noqa: BLE001
                    print(f"[bridge] error reading karaoke {p.name}: {exc}")


# --- HTTP server ---

class LiveHandler(BaseHTTPRequestHandler):
    state: BridgeState  # set by the server factory

    def _send_json(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        # Serve OnSong-compatible JSON at root (/) so G2Chords / G2Bridge
        # can connect with just the IP address (no path, no port in UI).
        # /api/live and /healthz are kept for compatibility.
        if self.path in ("/", "/api/live"):
            payload = self.state.get_payload()
            body = json.dumps(payload).encode("utf-8")
            self._send_json(200, body)
        elif self.path == "/healthz":
            body = b'{"status":"ok"}\n'
            self._send_json(200, body)
        else:
            body = b'{"error":"not found"}\n'
            self._send_json(404, body)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
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
    """Seed from any existing karaoke XML files so we're not empty on start."""
    cache_path = Path(cache_dir)
    if not cache_path.exists():
        return
    karaoke_files = []
    for f in cache_path.iterdir():
        if not f.is_file():
            continue
        if classify_karaoke(f):
            karaoke_files.append(f)

    if not karaoke_files:
        return

    # Use the most recently modified karaoke file as the seed.
    karaoke_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    f = karaoke_files[0]
    try:
        with open(f, "r", errors="replace") as fh:
            state.activate_from_karaoke(fh.read(), f.stem)
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

    # Seed from existing karaoke files.
    seed_existing(state, cache_dir)
    if state.song_start_ms is not None:
        print(f"[bridge] seeded from existing cache: {state.current_song_title}")

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
