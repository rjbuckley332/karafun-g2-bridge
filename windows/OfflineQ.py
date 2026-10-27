import sqlite3
import os
import shutil
import tempfile

source_db = os.path.expandvars(r"%APPDATA%\KaraFun\user_database.db")
source_wal = os.path.expandvars(r"%APPDATA%\KaraFun\user_database.db-wal")

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

    print("\n--- OFFLINE QUEUED SONGS ---")
    if not rows:
        print("Queue is empty.")
    else:
        for idx, (item_id, singer, title, artist, bpm) in enumerate(rows, 1):
            singer_display = f" (Singer: {singer})" if singer else ""
            bpm_display = f" [BPM: {bpm}]" if bpm else ""
            print(f"{idx}. \"{title}\" by {artist}{singer_display}{bpm_display}")

    conn.close()

finally:
    shutil.rmtree(temp_dir, ignore_errors=True)