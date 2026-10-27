"""Karafun (Windows) → G2Bridge OnSong-shaped lyric bridge (queue-aware, lyrics folder).

Reads the Karafun queue from user_database.db, ensures each song has a matching
lyrics file in the lyrics folder (creating "-blank" placeholders for missing ones),
and serves an OnSong-compatible /api/live JSON endpoint so G2Bridge / G2Karafun
can drive the Even G2 teleprompter.

ALSO serves a lyrics folder over HTTP so the G2Teleprompter EvenHub app
can fetch lyrics for any song without editing config.

Pure Python 3 stdlib.

Run:
    python karafun_queue_bridge.py --config config.json

Example config.json:
{
  "appdata_dir": "C:/Users/susqu/AppData/Roaming",
  "lyrics_dir": "C:/Users/susqu/OneDrive/Documents/KaraFunLyrics",
  "host": "0.0.0.0",
  "port": 8766,
  "queue_interval_sec": 5.0,
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
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

__version__ = "3.0.0"

# single-instance guard: Task Scheduler "Run" must not spawn a duplicate
if os.name == "nt":
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\KaraFunBridge")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        raise SystemExit(0)

LINES_PER_SCREEN = 4

# --- Phone control page (queue + talk/sing + skip) ---

TALK_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>KaraFun</title>
<style>
  body { background:#111; color:#eee; font-family:system-ui; margin:0; padding:5vh 4vw 8vh; }
  #now { font-size:22px; font-weight:600; margin-bottom:6px; }
  #now small { opacity:.6; font-weight:400; }
  .btns { display:flex; flex-direction:column; gap:14px; margin:22px 0 28px; }
  button { font-size:32px; padding:26px 0; border:none; border-radius:16px; color:#fff; }
  #toggle { background:#27ae60; }
  #next { background:#2980b9; font-size:24px; padding:18px 0; }
  h2 { font-size:17px; opacity:.7; margin:0 0 8px; }
  #q { list-style:none; margin:0; padding:0; }
  #q li { padding:10px 12px; background:#1d1d1d; border-radius:10px; margin-bottom:8px; font-size:18px; }
  #q li span { opacity:.55; font-size:14px; }
</style></head>
<body>
<div id="now">…</div>
<div class="btns">
  <button id="start" style="background:#27ae60">▶ START next song</button>
  <button id="toggle" style="background:#c9a227">⏯ Pause / Resume</button>
  <button id="next" style="background:#2980b9">⏭ Skip</button>
</div>
<div id="st" style="min-height:22px;font-size:16px;opacity:.85"></div>
<h2>Up next</h2>
<ul id="q"></ul>
<script>
async function refresh() {
  try {
    const [live, q] = await Promise.all([
      fetch('/api/live').then(r => r.json()),
      fetch('/queue').then(r => r.json())
    ]);
    document.getElementById('now').innerHTML = '🎤 ' + (live.songTitle || '—') + (live.lines && live.lines.length ? '' : ' <small>(no lyrics yet)</small>');
    const ul = document.getElementById('q'); ul.innerHTML = '';
    (q.queue || []).forEach((s, i) => {
      const li = document.createElement('li');
      li.innerHTML = (i+1) + '. ' + s.title + ' <span>' + (s.artist || '') + '</span>';
      ul.appendChild(li);
    });
  } catch (e) {}
}
function cmd(x){ fetch('/control/'+x).then(r=>r.json()).then(d=>{ document.getElementById('st').textContent = d.ok ? '✓ sent (' + d.song + ')' : '✗ ' + d.msg; }).catch(()=>{ document.getElementById('st').textContent = '✗ no response'; }); }
document.getElementById('start').onclick = () => { cmd('start'); setTimeout(refresh, 1500); };
document.getElementById('toggle').onclick = () => cmd('playpause');
document.getElementById('next').onclick = () => { cmd('next'); setTimeout(refresh, 1200); };
refresh();
setInterval(refresh, 4000);
</script>
</body></html>"""


def build_live_payload(song_title: str, key: str, section: int,
                       section_name: str, timeline: list[dict],
                       now_ms: int, fallback_line: str = "") -> dict:
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


def query_queue(appdata_dir: str) -> list[dict]:
    source_db = os.path.join(appdata_dir, "KaraFun", "user_database.db")
    source_wal = source_db + "-wal"

    if not os.path.exists(source_db):
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
    except Exception as exc:
        print(f"[bridge] error querying queue: {exc}")
        return []
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _normalize_title(title: str) -> str:
    """Normalize a song title for fuzzy matching: lowercase, strip spaces/special chars."""
    return re.sub(r'[^a-z0-9]', '', title.lower())


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename."""
    keep = "".join(c for c in name if c.isalnum() or c in " -_.")
    return keep.strip().replace(" ", "_")


# --- Bridge state ---

class BridgeState:
    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.Lock()
        self.timeline: list[dict] = []
        self.song_start_ms: float | None = None
        self.current_song_title: str = config.get("song_title", "Karafun Song")
        self.current_artist: str = ""
        self.payload: dict = build_live_payload(
            song_title=self.current_song_title,
            key=config.get("key", "C"),
            section=1,
            section_name="",
            timeline=[],
            now_ms=0,
            fallback_line=config.get("fallback_line", ""),
        )
        self._song_timelines: dict[str, list[dict]] = {}
        self._lyrics_dir: Path | None = None

        lyrics_dir = config.get("lyrics_dir", "")
        if lyrics_dir and Path(lyrics_dir).exists():
            self._lyrics_dir = Path(lyrics_dir)
            self._index_lyrics_folder()

    def _index_lyrics_folder(self) -> None:
        """Scan the lyrics folder and index all .txt files by normalized song title."""
        if not self._lyrics_dir or not self._lyrics_dir.exists():
            return
        count = 0
        for f in self._lyrics_dir.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in (".txt", ".lrc"):
                continue
            stem = f.stem
            # Skip "-blank" placeholder files
            if stem.endswith("-blank"):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                lines = [{"ms": i * 1000, "text": l.strip()} for i, l in enumerate(text.splitlines()) if l.strip()]
                if lines:
                    # Index by exact stem AND by normalized title for fuzzy matching
                    self._song_timelines[stem] = lines
                    norm = _normalize_title(stem)
                    if norm != stem:
                        self._song_timelines[norm] = lines
                    count += 1
            except Exception as exc:
                print(f"[bridge] error reading lyrics {f.name}: {exc}")
        print(f"[bridge] indexed {count} lyric files from {self._lyrics_dir}")

    def reload_lyrics_folder(self) -> None:
        """Re-scan the lyrics folder for new/changed files."""
        self._song_timelines.clear()
        self._index_lyrics_folder()

    def _ensure_placeholder(self, title: str, artist: str) -> None:
        """Create a placeholder lyrics file for a song if one doesn't exist."""
        if not self._lyrics_dir:
            return

        # Check if a lyrics file already exists (fuzzy match)
        norm = _normalize_title(title)
        for key in self._song_timelines:
            if key == title or key == norm:
                return  # already exists

        # Also check for exact filename match
        safe_filename = f"{artist} - {title}.txt"
        if (self._lyrics_dir / safe_filename).exists():
            return

        # Create placeholder with "-blank"
        blank_filename = f"{artist} - {title}-blank.txt"
        blank_path = self._lyrics_dir / blank_filename
        try:
            blank_path.write_text("", encoding="utf-8")
            print(f"[bridge] created placeholder: {blank_filename}")
        except Exception as exc:
            print(f"[bridge] error creating placeholder {blank_filename}: {exc}")

    def sync_lyrics_with_queue(self, appdata_dir: str) -> None:
        """Read the queue and ensure each song has a matching lyrics file."""
        queue = query_queue(appdata_dir)
        if not queue:
            return

        for song in queue:
            title = song.get("title", "")
            artist = song.get("artist", "")
            if title:
                self._ensure_placeholder(title, artist)

        # Re-index after creating placeholders
        self.reload_lyrics_folder()

    def refresh_queue(self, appdata_dir: str) -> None:
        queue = query_queue(appdata_dir)
        if not queue:
            return

        current = queue[0]
        title = current.get("title", "")
        artist = current.get("artist", "")

        # Try exact match first, then normalized (fuzzy) match
        with self.lock:
            matched_key = None
            if title and title in self._song_timelines:
                matched_key = title
            else:
                norm = _normalize_title(title)
                if norm and norm in self._song_timelines:
                    matched_key = norm
                    title = matched_key  # use the matched key for display

            if matched_key:
                if title != self.current_song_title:
                    self.current_song_title = title
                    self.current_artist = artist or ""
                    self.timeline = self._song_timelines[matched_key]
                    self.song_start_ms = time.time() * 1000
                    self.payload = build_live_payload(
                        song_title=title,
                        key=self.config.get("key", "C"),
                        section=1,
                        section_name="",
                        timeline=self.timeline,
                        now_ms=0,
                        fallback_line=self.config.get("fallback_line", ""),
                    )
                    print(f"[bridge] activated song from queue: {title} — {artist} ({len(self.timeline)} lines)")

    def get_payload(self) -> dict:
        with self.lock:
            # For lyrics folder files (no real timestamps), return all lines without time-based advance
            # Time-based advance only works for .kfn files with real timestamps
            if self.timeline:
                return {
                    "songTitle": self.current_song_title,
                    "key": self.config.get("key", "C"),
                    "section": 1,
                    "sectionName": "",
                    "lines": [{"parts": [ev["text"]]} for ev in self.timeline],
                }
            return dict(self.payload)

    def get_lyrics_for_song(self, song_title: str) -> list[dict] | None:
        """Get lyric lines for a song from the indexed folder."""
        with self.lock:
            if song_title in self._song_timelines:
                return [{"parts": [line]} for line in self._song_timelines[song_title]]
        return None

    def get_queue(self) -> list[dict]:
        """Return the current queue (cached, refreshed periodically)."""
        return query_queue(self.config.get("appdata_dir", ""))


# --- HTTP server ---

class LiveHandler(BaseHTTPRequestHandler):
    state: BridgeState

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

    def _send_file(self, path: Path) -> None:
        """Serve a file from the lyrics folder."""
        if not path.exists() or not path.is_file():
            body = b'{"error":"not found"}\n'
            self._send_json(404, body)
            return
        try:
            data = path.read_bytes()
        except Exception as exc:
            body = f'{{"error":"{exc}"}}\n'.encode("utf-8")
            self._send_json(500, body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        from urllib.parse import unquote, urlparse
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ("/", "/api/live"):
            payload = self.state.get_payload()
            body = json.dumps(payload).encode("utf-8")
            self._send_json(200, body)
        elif path == "/healthz":
            body = b'{"status":"ok"}\n'
            self._send_json(200, body)
        elif path == "/queue":
            queue = self.state.get_queue()
            body = json.dumps({"queue": queue}).encode("utf-8")
            self._send_json(200, body)
        elif path == "/api/songs":
            songs = []
            with self.state.lock:
                for title in self.state._song_timelines:
                    songs.append({"title": title})
            body = json.dumps({"songs": songs}).encode("utf-8")
            self._send_json(200, body)
        elif path == "/talk":
            self._send_talk_page()
        elif path.startswith("/control/"):
            self._handle_control(path[len("/control/"):])
        elif path.startswith("/lyrics/"):
            filename = path[len("/lyrics/"):]
            lyrics_dir = self.state._lyrics_dir
            if lyrics_dir:
                file_path = lyrics_dir / filename
                self._send_file(file_path)
            else:
                body = b'{"error":"lyrics dir not configured"}\n'
                self._send_json(500, body)
        else:
            body = b'{"error":"not found"}\n'
            self._send_json(404, body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _send_talk_page(self) -> None:
        data = TALK_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_control(self, cmd: str) -> None:
        """Proxy playback control to the user-session helper (the SYSTEM bridge
        service can't inject keystrokes into the interactive desktop)."""
        cmd = cmd.strip("/").lower()
        if cmd in ("play", "pause", "toggle"):
            cmd = "playpause"
        if cmd not in ("playpause", "next", "start", "enter"):
            self._send_json(400, b'{"ok":false,"msg":"unknown command"}\n')
            return
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:8769/{cmd}", timeout=3) as r:
                helper = json.loads(r.read().decode())
            ok = bool(helper.get("ok"))
            msg = "sent" if ok else "helper refused"
        except Exception:
            ok = False
            msg = "control helper not running (user session)"
        body = json.dumps({"ok": ok, "msg": msg, "song": self.state.current_song_title}).encode("utf-8")
        self._send_json(200, body)

    def log_message(self, fmt, *args):
        return


def load_config(config_path: str | None) -> dict:
    config = {
        "appdata_dir": os.environ.get("KARAFUN_APPDATA_DIR", ""),
        "lyrics_dir": os.environ.get("KARAFUN_LYRICS_DIR", ""),
        "host": os.environ.get("KARAFUN_HOST", "0.0.0.0"),
        "port": int(os.environ.get("KARAFUN_PORT", "8766")),
        "queue_interval_sec": float(os.environ.get("KARAFUN_QUEUE_POLL", "5.0")),
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
    parser = argparse.ArgumentParser(description="Karafun (Windows) → G2Bridge OnSong-shaped lyric bridge (queue-aware)")
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--appdata-dir", help="override Windows AppData directory")
    parser.add_argument("--lyrics-dir", help="override lyrics folder directory")
    parser.add_argument("--port", type=int, default=8766, help="HTTP port")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.appdata_dir:
        config["appdata_dir"] = args.appdata_dir
    if args.lyrics_dir:
        config["lyrics_dir"] = args.lyrics_dir
    if args.port:
        config["port"] = args.port

    appdata_dir = config.get("appdata_dir", "")
    lyrics_dir = config.get("lyrics_dir", "")

    if not appdata_dir or not Path(appdata_dir).exists():
        print(f"[bridge] appdata_dir not found: {appdata_dir}")
        print("       Set config.json appdata_dir to your Windows AppData folder")
        return 1

    if not lyrics_dir or not Path(lyrics_dir).exists():
        print(f"[bridge] lyrics_dir not found: {lyrics_dir}")
        print("       Set config.json lyrics_dir to your KaraFunLyrics folder")
        return 1

    state = BridgeState(config)
    LiveHandler.state = state

    # Sync lyrics with queue on startup
    print("[bridge] syncing lyrics with queue...")
    state.sync_lyrics_with_queue(appdata_dir)

    # Initial queue refresh
    state.refresh_queue(appdata_dir)

    # Start the queue refresher
    def queue_refresh_loop():
        while True:
            time.sleep(config.get("queue_interval_sec", 5.0))
            try:
                state.reload_lyrics_folder()   # pick up newly added lyric files without a restart
                state.refresh_queue(appdata_dir)
            except Exception as exc:
                print(f"[bridge] queue refresh error: {exc}")

    refresher = threading.Thread(target=queue_refresh_loop, daemon=True)
    refresher.start()
    print(f"[bridge] queue refresher started (interval={config.get('queue_interval_sec', 5.0)}s)")

    # Start HTTP server
    server = ThreadingHTTPServer((config["host"], config["port"]), LiveHandler)
    print(f"[bridge] HTTP on http://{config['host']}:{config['port']}/api/live")
    print(f"[bridge] Lyrics folder: {lyrics_dir}")
    print(f"[bridge] Queue endpoint: http://{config['host']}:{config['port']}/queue")
    print(f"[bridge] Songs endpoint: http://{config['host']}:{config['port']}/api/songs")
    print(f"[bridge] Lyrics endpoint: http://{config['host']}:{config['port']}/lyrics/<filename>.txt")
    print("[bridge] point G2Bridge at /api/live. Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] shutting down...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
