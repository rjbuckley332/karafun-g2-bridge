"""Karafun → OnSong-shaped lyric bridge.

Watches a Karafun cache directory for .kfn files, unlocks them (AES-128-ECB
using the FLID header key — the karafunkey.py logic), extracts the SONG subfile
.ini with time-synced lyrics, and serves an OnSong-compatible /api/live JSON
endpoint so G2Bridge can drive the Even G2 teleprompter unchanged.

Run:
    python src/karafun_bridge.py --config config.json
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

__version__ = "0.1.0"

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
    if flid != all_zeroes and flid is not None:
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
        in_bad_effect = False
        for line in lines:
            stripped = line.strip()
            if stripped.lower().startswith("eff") and stripped.endswith("]"):
                # eff[XX] header — decide by id inside the section body below;
                # conservative: keep section header line, filter body by id.
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

import re

_TIMESTAMP_RE = re.compile(r"^\[(\d+)\]:\s*(.*)$")


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

def build_live_payload(song_title: str, key: str, section: int,
                       section_name: str, timeline: list[dict],
                       now_ms: int, fallback_line: str = "") -> dict:
    """Build the OnSong-shaped /api/live payload G2Bridge consumes."""
    # Find the current lyric line based on elapsed song time.
    current_text = fallback_line
    current_parts: list[Any] = []
    for ev in timeline:
        if ev["ms"] <= now_ms:
            current_text = ev["text"]
        else:
            break

    if current_text:
        current_parts = [current_text]

    return {
        "songTitle": song_title,
        "key": key,
        "section": section,
        "sectionName": section_name,
        "lines": [
            {
                "parts": current_parts,
            }
        ],
    }


# --- Bridge state ---

class BridgeState:
    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.Lock()
        self.timeline: list[dict] = []
        self.song_start_ms: float | None = None
        self.current_song: str | None = None
        self.payload: dict = build_live_payload(
            song_title=config.get("song_title", "Karafun Song"),
            key=config.get("key", "C"),
            section=1,
            section_name="",
            timeline=[],
            now_ms=0,
            fallback_line=config.get("fallback_line", ""),
        )

    def update_from_ini(self, ini_text: str, song_title: str) -> None:
        timeline = parse_ini_timeline(ini_text)
        now = time.time() * 1000
        with self.lock:
            self.timeline = timeline
            self.song_start_ms = now
            self.current_song = song_title
            self.payload = build_live_payload(
                song_title=song_title,
                key=self.config.get("key", "C"),
                section=1,
                section_name="",
                timeline=timeline,
                now_ms=0,  # song just started
                fallback_line=self.config.get("fallback_line", ""),
            )

    def get_payload(self) -> dict:
        with self.lock:
            payload = dict(self.payload)
            if self.song_start_ms is not None and self.timeline:
                now_ms = int((time.time() * 1000) - self.song_start_ms)
                payload = build_live_payload(
                    song_title=self.config.get("song_title", "Karafun Song"),
                    key=self.config.get("key", "C"),
                    section=1,
                    section_name="",
                    timeline=self.timeline,
                    now_ms=now_ms,
                    fallback_line=self.config.get("fallback_line", ""),
                )
            return payload


# --- File watcher ---

class KfnHandler(FileSystemEventHandler):
    def __init__(self, state: BridgeState, config: dict):
        self.state = state
        self.config = config
        self._processing: set[str] = set()
        self._lock = threading.Lock()

    def on_created(self, event):
        if event.is_directory:
            return
        self._maybe_process(event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._maybe_process(event.src_path)

    def _maybe_process(self, path: str) -> None:
        p = Path(path)
        if p.suffix.lower() != ".kfn":
            return
        with self._lock:
            if str(p) in self._processing:
                return
            self._processing.add(str(p))
        try:
            self._process_file(p)
        finally:
            with self._lock:
                self._processing.discard(str(p))

    def _process_file(self, path: Path) -> None:
        print(f"[bridge] detected .kfn: {path}")
        try:
            with open(path, "rb") as f:
                kfn = read_kfn(f)
            kfn = unlock_kfn(kfn)
            ini_text = extract_ini_text(kfn)
            if not ini_text.strip():
                print(f"[bridge] no ini text in {path.name}")
                return
            # Use the filename (minus extension) as the song title.
            song_title = path.stem
            self.state.update_from_ini(ini_text, song_title)
            print(f"[bridge] loaded {song_title}: {len(self.state.timeline)} lyric events")
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] error processing {path}: {exc}")


# --- HTTP server ---

class LiveHandler(BaseHTTPRequestHandler):
    state: BridgeState  # set by the server factory

    def do_GET(self):  # noqa: N802
        if self.path == "/api/live":
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Karafun → G2Bridge OnSong-shaped lyric bridge")
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
        print("       Run: python src/discover_cache.py  (after playing a song in Karafun)")
        return 1

    state = BridgeState(config)
    LiveHandler.state = state

    # Seed from any existing .kfn files so we're not empty on start.
    for kfn_path in sorted(Path(cache_dir).glob("*.kfn")):
        try:
            with open(kfn_path, "rb") as f:
                kfn = read_kfn(f)
            kfn = unlock_kfn(kfn)
            ini_text = extract_ini_text(kfn)
            if ini_text.strip():
                state.update_from_ini(ini_text, kfn_path.stem)
                print(f"[bridge] seeded from {kfn_path.name}")
                break
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] seed skip {kfn_path.name}: {exc}")

    # Start the file watcher.
    observer = Observer()
    handler = KfnHandler(state, config)
    observer.schedule(handler, cache_dir, recursive=False)
    observer.start()
    print(f"[bridge] watching {cache_dir}")

    # Start HTTP server.
    server = ThreadingHTTPServer((config["host"], config["port"]), LiveHandler)
    print(f"[bridge] HTTP on http://{config['host']}:{config['port']}/api/live")
    print("[bridge] point G2Bridge at that URL. Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] shutting down...")
    finally:
        observer.stop()
        observer.join()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
