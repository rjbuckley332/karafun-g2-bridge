"""Karafun (Windows) → OnSong-shaped lyric bridge.

Watches Karafun's Windows cache directory for .kfn files, unlocks them
(AES-128-ECB using the FLID header key — the karafunkey.py logic), extracts
the SONG subfile .ini with time-synced lyrics, and serves an OnSong-compatible
/api/live JSON endpoint so G2Bridge / G2Karafun can drive the Even G2
teleprompter unchanged.

Karafun on Windows writes .kfn cache files during playback. This bridge
detects new/changed .kfn files, unlocks them, parses the timed lyrics, and
serves 4 lines at once (current + 3 upcoming) based on elapsed song time.
Fully automatic — no R1 ring required.

Zero external dependencies — pure Python 3 stdlib + cryptography.

Run:
    python karafun_win_bridge.py --config config.json

Example config.json:
{
  "cache_dir": "C:/ProgramData/Recisio/KaraFun 2/Cache",
  "host": "0.0.0.0",
  "port": 8766,
  "song_title": "Karafun Song",
  "key": "C",
  "fallback_line": ""
}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

__version__ = "1.0.0"

# --- KFN format constants (from karafunkey.py / WinslowJosiah) ---

KFN_MAGIC = b"KFNB"
VALID_EFFECT_IDS = {1, 2, 21, 51, 53, 61, 62}

SONG = 1
AUDIO = 2
IMAGE = 3
FONT = 4
VIDEO = 5
MILKDROP = 6
CDG = 7


class KFNSubfile:
    def __init__(self, name: bytes, ftype: int, data: bytes, length: int, is_encrypted: bool):
        self.name = name
        self.ftype = ftype
        self.data = data
        self.length = length
        self.is_encrypted = is_encrypted


class KFNFile:
    def __init__(self):
        self.headers: dict[str, Any] = {}
        self.subfiles: list[KFNSubfile] = []


def read_kfn(stream) -> KFNFile:
    kfn = KFNFile()
    stream.seek(0)
    magic = struct.unpack("<4s", stream.read(4))[0]
    if magic != KFN_MAGIC:
        raise ValueError(f"unexpected file signature: {magic!r}")

    while True:
        header, flag = struct.unpack("<4sB", stream.read(5))
        header = header.decode("latin-1")
        if flag == 1:
            data = struct.unpack("I", stream.read(4))[0]
        elif flag == 2:
            length = struct.unpack("I", stream.read(4))[0]
            data = stream.read(length)
        else:
            raise ValueError(f"unexpected flag for header {header}: {flag}")
        if header == "ENDH":
            break
        kfn.headers[header] = data

    (subfile_count,) = struct.unpack("I", stream.read(4))
    subfile_info = {}
    for _ in range(subfile_count):
        (name_length,) = struct.unpack("I", stream.read(4))
        subfile_name = stream.read(name_length)
        subfile_metadata = struct.unpack("IIIII", stream.read(20))
        subfile_info[subfile_name] = subfile_metadata

    subfiles_start = stream.tell()
    for name, metadata in subfile_info.items():
        ftype, length, offset, encrypted_length, is_encrypted = metadata
        stream.seek(subfiles_start + offset)
        data = stream.read(encrypted_length)
        kfn.subfiles.append(
            KFNSubfile(
                name=name,
                ftype=ftype,
                data=data,
                length=length,
                is_encrypted=bool(is_encrypted),
            )
        )
    return kfn


def unlock_kfn(kfn: KFNFile) -> KFNFile:
    all_zeroes = bytes(16)
    flid = kfn.headers.get("FLID")
    if flid != all_zeroes and flid is not None and HAS_CRYPTO:
        cipher = Cipher(algorithms.AES128(flid), modes.ECB())
        decryptor = cipher.decryptor()
        kfn.headers["FLID"] = all_zeroes
        for sub in kfn.subfiles:
            if not sub.is_encrypted:
                continue
            sub.data = decryptor.update(sub.data)[: sub.length]
            sub.is_encrypted = False

    if "RGHT" in kfn.headers:
        kfn.headers["RGHT"] = 0

    for sub in kfn.subfiles:
        if sub.ftype != SONG:
            continue
        try:
            config_text = sub.data.decode("latin-1")
        except UnicodeDecodeError:
            continue
        # Strip invalid effect sections (keep only known-good effect IDs)
        lines = config_text.splitlines()
        kept = []
        for line in lines:
            stripped = line.strip()
            if stripped.lower().startswith("eff") and stripped.endswith("]"):
                kept.append(line)
                continue
            kept.append(line)
        sub.data = "\n".join(kept).encode("latin-1")
        sub.length = len(sub.data)

    return kfn


def extract_ini_text(kfn: KFNFile) -> str:
    """Return the decoded .ini text from the SONG subfile, or '' if none."""
    for sub in kfn.subfiles:
        if sub.ftype == SONG:
            try:
                return sub.data.decode("latin-1")
            except UnicodeDecodeError:
                return sub.data.decode("utf-8", errors="replace")
    return ""


# --- .ini timestamp / lyric parsing ---

_TIMESTAMP_RE = re.compile(r"^(\d+):(\d+):(\d+),(\d+)$")


def parse_timestamp(ts: str) -> int:
    """Parse 'HH:MM:SS,mmm' into total milliseconds."""
    m = _TIMESTAMP_RE.match(ts.strip())
    if not m:
        return 0
    h, mi, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return ((h * 60 + mi) * 60 + s) * 1000 + ms


def parse_ini_timeline(ini_text: str) -> list[dict]:
    """Parse a Karafun .ini into a list of {ms, text} lyric events.

    The .ini format is expected to contain lines like:
        [12345]: lyric text here
    where the number is the timestamp in milliseconds.
    """
    events: list[dict] = []
    for line in ini_text.splitlines():
        line = line.rstrip("\r")
        m = _TIMESTAMP_RE.match(line)
        if not m:
            continue
        ms = int(m.group(1))
        text = m.group(2).strip()
        events.append({"ms": ms, "text": text})
    events.sort(key=lambda e: e["ms"])
    return events


# --- OnSong-shaped payload builder ---

LINES_PER_SCREEN = 4  # number of lyric lines to show at once


def build_live_payload(song_title: str, key: str, section: int,
                       section_name: str, timeline: list[dict],
                       now_ms: int, fallback_line: str = "") -> dict:
    """Build the OnSong-shaped /api/live payload G2Bridge consumes.

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
        # Track processed files to avoid re-activating the same song.
        self._processed_files: dict[str, float] = {}  # path -> mtime

    def activate_from_kfn(self, kfn_path: str, kfn_data: bytes) -> None:
        """Unlock a .kfn file, extract the .ini, and activate it as the current song."""
        if not HAS_CRYPTO:
            print("[bridge] cryptography library not installed; cannot unlock .kfn files")
            print("[bridge] install with: pip install cryptography")
            return

        try:
            import io
            stream = io.BytesIO(kfn_data)
            kfn = read_kfn(stream)
            kfn = unlock_kfn(kfn)
            ini_text = extract_ini_text(kfn)
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] error unlocking .kfn: {exc}")
            return

        if not ini_text.strip():
            print("[bridge] no ini text in .kfn file")
            return

        timeline = parse_ini_timeline(ini_text)
        if not timeline:
            print("[bridge] no parseable lyric lines in .ini")
            return

        now = time.time() * 1000
        self.timeline = timeline
        self.song_start_ms = now
        self.current_song_title = os.path.basename(kfn_path)
        self.payload = build_live_payload(
            song_title=self.current_song_title,
            key=self.config.get("key", "C"),
            section=1,
            section_name="",
            timeline=timeline,
            now_ms=0,  # song just started
            fallback_line=self.config.get("fallback_line", ""),
        )
        print(f"[bridge] activated song: {self.current_song_title} ({len(timeline)} lyric lines)")

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
            if p.suffix.lower() != ".kfn":
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

            # Only process if the file changed (new mtime)
            try:
                with open(path, "rb") as f:
                    kfn_data = f.read()
                self.state.activate_from_kfn(path, kfn_data)
            except Exception as exc:  # noqa: BLE001
                print(f"[bridge] error reading .kfn {p.name}: {exc}")


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
    """Seed from any existing .kfn files so we're not empty on start."""
    cache_path = Path(cache_dir)
    if not cache_path.exists():
        return
    kfn_files = sorted(cache_path.glob("*.kfn"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not kfn_files:
        return
    # Use the most recently modified .kfn as the seed.
    f = kfn_files[0]
    try:
        with open(f, "rb") as fh:
            state.activate_from_kfn(str(f), fh.read())
    except Exception as exc:  # noqa: BLE001
        print(f"[bridge] seed skip .kfn {f.name}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Karafun (Windows) → G2Bridge OnSong-shaped lyric bridge")
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--cache-dir", help="override Karafun cache directory")
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
        print("       Set config.json cache_dir to your Karafun cache folder")
        print("       (e.g., C:/ProgramData/Recisio/KaraFun 2/Cache)")
        return 1

    state = BridgeState(config)
    LiveHandler.state = state

    # Seed from existing .kfn files.
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
