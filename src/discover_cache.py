"""Discover where Karafun writes its .kfn cache files on macOS.

Usage:
    python src/discover_cache.py

This walks likely Application Support locations and prints any directory
containing .kfn files, plus the most recently modified candidates.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HOME = Path.home()
CANDIDATES = [
    HOME / "Library" / "Application Support" / "KaraFun",
    HOME / "Library" / "Application Support" / "KaraFun" / "Cache",
    HOME / "Library" / "Containers" / "com.karafun.karafun" / "Data" / "Library" / "Application Support" / "KaraFun",
    HOME / "Library" / "Caches" / "KaraFun",
    HOME / "Library" / "Caches" / "com.karafun.karafun",
    Path("/Library/Application Support/KaraFun"),
    Path("/Library/Application Support/KaraFun/Cache"),
]


def find_kfn_dirs(root: Path) -> list[Path]:
    """Return directories under root that contain .kfn files (shallow scan)."""
    hits: list[Path] = []
    try:
        for entry in root.iterdir():
            if entry.is_dir():
                kfn_count = sum(1 for f in entry.iterdir() if f.suffix.lower() == ".kfn")
                if kfn_count:
                    hits.append(entry)
    except OSError:
        pass
    return hits


def main() -> int:
    print("Scanning likely Karafun locations on this Mac...\n")
    found_any = False

    for candidate in CANDIDATES:
        if not candidate.exists():
            print(f"[absent] {candidate}")
            continue
        kfn_dirs = find_kfn_dirs(candidate)
        if kfn_dirs:
            found_any = True
            for d in kfn_dirs:
                kfns = sorted(d.glob("*.kfn"), key=lambda p: p.stat().st_mtime, reverse=True)
                print(f"[found .kfn in] {d}")
                for f in kfns[:5]:
                    mtime = f.stat().st_mtime
                    print(f"      {f.name}  ({f.stat().st_size} bytes, modified {mtime})")
        else:
            print(f"[empty/no .kfn] {candidate}")

    if not found_any:
        print("\nNo .kfn files found in known locations.")
        print(
            "Tip: launch Karafun, play/download a song, then re-run this script. "
            "Karafun writes cache files only while streaming."
        )
        return 1

    print("\nDone. Point config.json `cache_dir` at the directory containing the .kfn files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
