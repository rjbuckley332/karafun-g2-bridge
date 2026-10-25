#!/usr/bin/env python3
"""Watch Karafun's Mac container for new cache files while a song streams.

Polls the container and reports any new/modified files so we can identify
where the live song cache lands (and in what format: .kfn, XML, Ogg, etc.).
"""
import os
import sys
import time
from pathlib import Path

CONTAINER = Path.home() / "Library" / "Containers" / "com.recisio.kfiphone"
DATA = CONTAINER / "Data"


def snapshot(root: Path) -> dict:
    """Return {path: (size, mtime)} for all files under root."""
    out = {}
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if p.is_file():
            try:
                st = p.stat()
                out[str(p)] = (st.st_size, st.st_mtime_ns)
            except OSError:
                pass
    return out


def main() -> int:
    if not DATA.exists():
        print(f"Karafun data dir not found: {DATA}")
        return 1

    print(f"Watching {DATA} for new/modified files...")
    print("Stream a song in Karafun now. I'll report new files as they appear.")
    print("=" * 60)

    before = snapshot(DATA)
    print(f"Baseline: {len(before)} files")
    last_report = time.time()

    try:
        while True:
            time.sleep(3)
            after = snapshot(DATA)

            new = set(after) - set(before)
            changed = {p for p in set(after) & set(before) if after[p] != before[p]}

            if new or changed:
                now = time.time()
                if now - last_report > 2 or True:
                    print(f"\n[{time.strftime('%H:%M:%S')}] Changes detected:")
                    for p in sorted(new):
                        size, _ = after[p]
                        print(f"  NEW   {p} ({size} bytes)")
                    for p in sorted(changed):
                        size, _ = after[p]
                        print(f"  MOD   {p} ({size} bytes)")
                    last_report = now

            before = after
    except KeyboardInterrupt:
        print("\nWatcher stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
