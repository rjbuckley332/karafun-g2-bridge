#!/usr/bin/env python3
"""KaraTonic - play the tonic note of each KaraFun song AS IT STARTS,
transposed by the singer's KaraFun pitch setting, for a fixed lead time
with gentle fades.

Runs on the Windows karaoke laptop. Trigger = new row in KaraFun's
history_item table (written at song start; carries song_pitch and joins
to song.keys). Effective key = song.keys (base) + song_pitch (semitones).

Audio uses winsound (stdlib) - runs in the interactive user session via
run-tonic.bat (stdin <nul, console hidden by task settings).

Usage:
    python tonic_player.py                  # watch mode
    python tonic_player.py --play-now G     # one-off 6s test tone
"""

from __future__ import annotations

import argparse, math, os, shutil, sqlite3, sys, tempfile, time, wave
from urllib.request import urlopen, Request
from pathlib import Path

NOTE_SEMITONES = {
    "C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4,
    "F": 5, "F#": 6, "GB": 6, "G": 7, "G#": 8, "AB": 8, "A": 9,
    "A#": 10, "BB": 10, "B": 11,
}
SEMITONE_NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
SAMPLE_RATE = 44100
LOG_PATH = Path(os.path.dirname(os.path.abspath(__file__))) / "tonic-player.log"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass  # pythonw/no-console: stdout is None
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass



def ensure_single_instance() -> None:
    """Global mutex so logon restarts can't pile up duplicate watchers."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, "Global\KaraTonicPlayer")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            log("another KaraTonic instance already running; exiting")
            sys.exit(0)

def key_to_freq(key_name: str, pitch: int, octave: int) -> float:
    base = key_name.strip().upper()
    if base not in NOTE_SEMITONES:
        raise ValueError(f"unknown key {key_name!r}")
    semi = (NOTE_SEMITONES[base] + int(pitch)) % 12 + (octave - 4) * 12
    return 440.0 * (2.0 ** ((semi - 9) / 12.0))


def transposed_name(key_name: str, pitch: int) -> str:
    base = key_name.strip().upper()
    if base not in NOTE_SEMITONES:
        return key_name
    return SEMITONE_NAMES[(NOTE_SEMITONES[base] + int(pitch)) % 12]


def generate_tone_wav(path: Path, freq: float, duration_sec: float,
                      volume: float, fade_sec: float) -> None:
    n = int(SAMPLE_RATE * duration_sec)
    fade = max(1, min(int(SAMPLE_RATE * fade_sec), n // 2))
    frames = bytearray()
    for i in range(n):
        s = math.sin(2 * math.pi * freq * (i / SAMPLE_RATE))
        if i < fade:
            s *= i / fade
        elif i >= n - fade:
            s *= (n - i) / fade
        frames += int(volume * 32767 * s).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(frames))


def read_latest_start(db_dir: str):
    """Newest history_item row = most recent song start.
    Returns {ts, title, artist, keys, pitch} or None."""
    src = os.path.join(db_dir, "KaraFun", "user_database.db")
    if not os.path.exists(src):
        return None
    tmpd = tempfile.mkdtemp()
    tmp = os.path.join(tmpd, "s.db")
    try:
        shutil.copy2(src, tmp)
        wal = src + "-wal"
        if os.path.exists(wal):
            shutil.copy2(wal, tmp + "-wal")
        c = sqlite3.connect(tmp)
        cur = c.cursor()
        cur.execute("""SELECT h.timestamp,
                              COALESCE(s.title, c.title)   AS title,
                              COALESCE(s.artist, c.artist) AS artist,
                              COALESCE(s.keys, c.keys)     AS keys,
                              h.song_pitch
                       FROM history_item h
                       LEFT JOIN song s      ON h.type_id = 1 AND h.ref_id = s.song_id
                       LEFT JOIN community c ON h.type_id = 2 AND h.ref_id = c.community_id
                       ORDER BY h.timestamp DESC LIMIT 1""")
        row = cur.fetchone()
        c.close()
        if not row:
            return None
        return {"ts": int(row[0]), "title": row[1] or "?", "artist": row[2] or "",
                "keys": row[3], "pitch": row[4] if row[4] is not None else 0}
    except Exception as exc:
        log(f"db read error: {exc}")
        return None
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def play_tonic(freq: float, lead_sec: float, volume: float, fade_sec: float,
               tmp_wav: Path) -> None:
    generate_tone_wav(tmp_wav, freq, lead_sec, volume, fade_sec)
    import winsound
    winsound.PlaySound(str(tmp_wav), winsound.SND_FILENAME | winsound.SND_ASYNC)


def helper_toggle(helper_url: str) -> bool:
    """Tap KaraFun play/pause via the kara_control_helper (127.0.0.1:8769)."""
    try:
        req = Request(helper_url.rstrip("/") + "/playpause")
        with urlopen(req, timeout=3) as resp:
            resp.read()
        return True
    except Exception as exc:
        log(f"helper toggle failed: {exc}")
        return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--lead-sec", type=float, default=10.0)
    p.add_argument("--fade-sec", type=float, default=0.7)
    p.add_argument("--volume", type=float, default=0.5)
    p.add_argument("--octave", type=int, default=3)
    p.add_argument("--poll-sec", type=float, default=1.0)
    p.add_argument("--key", help="force key for all songs (testing)")
    p.add_argument("--helper-url", default="http://127.0.0.1:8769",
                   help="kara_control_helper base URL (pause/resume)")
    p.add_argument("--no-pause", action="store_true",
                   help="do not pause KaraFun during the tonic")
    p.add_argument("--resong-sec", type=float, default=90.0,
                   help="min seconds before the SAME song can re-fire (default 90; resume rows are deduped by song identity, this is the backstop)")
    p.add_argument("--play-now", metavar="KEY", help="one-off test tone and exit")
    p.add_argument("--duration", type=float, default=6.0)
    args = p.parse_args()

    tmp_wav = Path(tempfile.gettempdir()) / "katatonic_tone.wav"

    if args.play_now:
        freq = key_to_freq(args.play_now, 0, args.octave)
        log(f"play-now: {args.play_now}{args.octave} = {freq:.1f} Hz, {args.duration}s")
        play_tonic(freq, args.duration, args.volume, args.fade_sec, tmp_wav)
        time.sleep(args.duration + 0.5)
        return 0

    ensure_single_instance()
    db_dir = os.path.expandvars(r"%APPDATA%")
    log(f"KaraTonic watch start lead={args.lead_sec}s fade={args.fade_sec}s "
        f"vol={args.volume} oct={args.octave} forced_key={args.key}")

    last_ts = None
    last_fired_sig = None
    last_fired_at = 0.0
    hb = 0
    try:
        while True:
            latest = read_latest_start(db_dir)
            if latest is not None:
                if last_ts is None:
                    last_ts = latest["ts"]  # seed: no fire for whatever last played
                    log(f"seeded at {latest['title']} ts={last_ts}")
                elif latest["ts"] > last_ts:
                    last_ts = latest["ts"]
                    song_sig = ((latest["title"] or "").strip().lower(),
                                latest["pitch"], (latest["keys"] or "").strip().lower())
                    now = time.monotonic()
                    if song_sig == last_fired_sig and now - last_fired_at < args.resong_sec:
                        log(f"suppress duplicate row for {latest['title']} "
                            f"(pause/resume echo, ts={latest['ts']})")
                        time.sleep(args.poll_sec)
                        hb += 1
                        continue
                    key_name = args.key or (latest["keys"] or "").strip()
                    if not key_name:
                        log(f"start: {latest['title']} - no key in db, skipping")
                    else:
                        pitch = latest["pitch"]
                        eff = transposed_name(key_name, pitch)
                        try:
                            freq = key_to_freq(key_name, pitch, args.octave)
                        except ValueError as exc:
                            log(f"start: {latest['title']} - {exc}")
                            time.sleep(args.poll_sec)
                            continue
                        log(f"start: {latest['title']} ({latest['artist']}) "
                            f"base={key_name} pitch={pitch} -> tonic {eff} {freq:.1f}Hz")
                        last_fired_sig = song_sig
                        last_fired_at = now
                        try:
                            paused = False
                            if not args.no_pause:
                                paused = helper_toggle(args.helper_url)
                                log(f"pause sent ok={paused}")
                                time.sleep(0.4)
                            play_tonic(freq, args.lead_sec, args.volume,
                                       args.fade_sec, tmp_wav)
                            time.sleep(args.lead_sec + 0.2)
                            if paused:
                                log("resume sent")
                                helper_toggle(args.helper_url)
                        except Exception as exc:
                            log(f"play error: {exc}")
            hb += 1
            if hb % max(1, int(30 / args.poll_sec)) == 0:
                cur = latest["title"] if latest else "none"
                curts = latest["ts"] if latest else 0
                log(f"alive last_ts={last_ts} latest={cur} ts={curts}")
            time.sleep(args.poll_sec)
    except KeyboardInterrupt:
        log("stopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
