"""Discover where Karafun writes its cache on macOS.

Karafun on Mac does NOT use .kfn files. It caches song metadata XML and
timed-karaoke XML in:
  ~/Library/Containers/com.recisio.kfiphone/Data/Library/Caches/com.recisio.kfiphone/fsCachedData

Usage:
    python src/discover_cache.py

This scans likely locations and reports directories containing cached song
XML (files with <song ...> or <karaoke ...> content).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HOME = Path.home()
CANDIDATES = [
    # Primary: the actual Karafun Mac container cache
    HOME / "Library" / "Containers" / "com.recisio.kfiphone" / "Data" / "Library" / "Caches" / "com.recisio.kfiphone" / "fsCachedData",
    # Fallback guesses
    HOME / "Library" / "Application Support" / "KaraFun",
    HOME / "Library" / "Application Support" / "KaraFun" / "Cache",
    HOME / "Library" / "Caches" / "KaraFun",
    Path("/Library/Application Support/KaraFun"),
]


def is_karafun_cache(path: Path) -> bool:
    """Return True if the directory contains Karafun song/karaoke XML files."""
    if not path.is_dir():
        return False
    try:
        for entry in path.iterdir():
            if not entry.is_file():
                continue
            try:
                with open(entry, "rb") as f:
                    head = f.read(80)
            except OSError:
                continue
            if b"<song" in head or b"<karaoke" in head:
                return True
    except OSError:
        pass
    return False


def main() -> int:
    print("Scanning likely Karafun cache locations on this Mac...\n")
    found_any = False

    for candidate in CANDIDATES:
        if not candidate.exists():
            print(f"[absent] {candidate}")
            continue
        if is_karafun_cache(candidate):
            found_any = True
            files = sorted(candidate.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            print(f"[found Karafun cache] {candidate}")
            print(f"      {len(files)} cached files")
            for f in files[:5]:
                st = f.stat()
                print(f"      {f.name}  ({st.st_size} bytes, modified {st.st_mtime})")
        else:
            print(f"[no Karafun cache] {candidate}")

    if not found_any:
        print("\nNo Karafun cache found in known locations.")
        print(
            "Tip: launch Karafun and stream a song, then re-run this script. "
            "Karafun writes cache XML only during playback."
        )
        return 1

    print("\nDone. Point config.json `cache_dir` at the fsCachedData directory above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
