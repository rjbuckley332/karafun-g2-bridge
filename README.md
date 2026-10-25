# karafun-g2-bridge

A macOS Python bridge that unlocks Karafun `.kfn` cache files and serves their
time-synced lyrics over a local HTTP endpoint shaped like OnSong's realtime
webserver — so your existing **G2Bridge** (which reads OnSong) can drive the
Even G2 teleprompter without any changes.

## Architecture

```
Karafun desktop (plays song)
  → writes encrypted .kfn to cache dir
  → karafun-g2-bridge watches that dir
  → unlocks .kfn (AES-128-ECB, karafunkey.py logic)
  → extracts the SONG subfile .ini (timed lyrics + timestamps)
  → serves /api/live (OnSong-shaped JSON)
  → G2Bridge polls /api/live → pushes to Even G2 teleprompter
```

G2Bridge stays untouched. The Even R1 ring still works for manual scroll/pause
on the glasses side.

## What's here

- `src/karafun_bridge.py` — file watcher + KFN unlocker + lyric parser + HTTP server
- `src/discover_cache.py` — helper to locate Karafun's macOS cache directory
- `requirements.txt` — Python deps (`watchdog`, `cryptography`)
- `config.example.json` — copy to `config.json` and fill in your paths

## Quick start

```bash
# 1. Install Karafun on the Mac and play a song (to create cache files)
# 2. Find where Karafun writes .kfn files on your Mac
python src/discover_cache.py

# 3. Configure
cp config.example.json config.json
# edit config.json: set cache_dir, port, etc.

# 4. Run the bridge
python src/karafun_bridge.py

# 5. Point G2Bridge at http://<mac-ip>:<port>/api/live
```

## Status

Phase 1 (scaffold) is done. The cache directory, `.ini` timestamp format, and
cache lifecycle on macOS still need to be confirmed with a live Karafun install
before the parser is final.
