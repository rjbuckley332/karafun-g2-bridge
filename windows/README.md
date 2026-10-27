# Karafun (Windows) → G2Bridge OnSong-shaped lyric bridge
# ============================================================
#
# This bridge watches Karafun's Windows cache directory for .kfn files,
# unlocks them (AES-128-ECB using the FLID header key), extracts the
# SONG subfile .ini with time-synced lyrics, and serves an OnSong-compatible
# /api/live JSON endpoint so G2Bridge / Even G2 teleprompter can read it.
#
# Pure Python 3 stdlib + cryptography. No external GUI tools needed.
#
# USAGE
# -----
#
# 1. Install Python 3 on your Windows laptop (if not already installed).
#
# 2. Install the cryptography dependency:
#      pip install cryptography
#
# 3. Find your Karafun cache directory on Windows.
#    Common locations:
#      C:/ProgramData/Recisio/KaraFun 2/Cache
#      C:/Users/<YourName>/AppData/Local/Recisio/KaraFun/Cache
#    Launch Karafun, play a song, then check which folder gets new .kfn files.
#
# 4. Create config.json (copy config.example.json → config.json) and set
#    cache_dir to your Karafun cache folder.
#
# 5. Run the bridge:
#      python karafun_win_bridge.py
#
# 6. Point G2Bridge (or G2Karafun app) at:
#      http://<your-windows-ip>:8766/api/live
#
#    The bridge serves OnSong-shaped JSON at both / and /api/live, so you
#    can connect with just the IP address if your client supports that.
#
# FILES
# -----
#   karafun_win_bridge.py   - the bridge script (this file)
#   config.json             - your configuration (create from config.example.json)
#   config.example.json     - example configuration template
#   requirements.txt        - Python dependencies (cryptography)
#
# NOTES
# -----
#   - The bridge polls the cache directory every 1 second by default.
#   - When a new or changed .kfn file is detected, it is unlocked and the
#     lyrics are served automatically. No manual action needed.
#   - The G2 teleprompter will show 4 lines at once (current + 3 upcoming).
#   - No R1 ring required — lyrics advance automatically with the song.
#
# TROUBLESHOOTING
# ---------------
#   - "cryptography library not installed" → run: pip install cryptography
#   - cache_dir not found → verify the path in config.json points to the
#     folder where Karafun writes .kfn files on your Windows machine
#   - No lyrics appearing → make sure you are actually playing a song in
#     Karafun (not just browsing). .kfn files are only written during playback.
