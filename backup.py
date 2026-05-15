import os
import shutil
import time

DB_FILE = "lap_timer.db"
BACKUP_DIR = "backups"


def backup_db(label="backup"):
    if not os.path.exists(DB_FILE):
        return None

    os.makedirs(BACKUP_DIR, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{label}_{timestamp}.db"
    backup_path = os.path.join(BACKUP_DIR, filename)

    shutil.copy2(DB_FILE, backup_path)

    print(f"DB BACKUP: {backup_path}")

    return backup_path