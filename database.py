import sqlite3
import time

DB_FILE = "lap_timer.db"


def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS laps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id INTEGER,
        name TEXT NOT NULL,
        major INTEGER NOT NULL,
        lap_number INTEGER NOT NULL,
        lap_time REAL NOT NULL,
        timestamp REAL NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS devices (
    mac_address TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model_name TEXT,
    team_name TEXT,
    major INTEGER DEFAULT 1,
    minor INTEGER UNIQUE,
    created_at REAL,
    updated_at REAL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS majors (
        major INTEGER PRIMARY KEY,
        label TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS races (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        status TEXT NOT NULL,
        started_at REAL,
        finished_at REAL,
        created_at REAL NOT NULL
    )
    """)

    try:
        cur.execute("""
        ALTER TABLE laps
        ADD COLUMN race_id INTEGER
        """)
    except Exception:
        pass

    cur.execute("""
    INSERT OR IGNORE INTO settings (key, value)
    VALUES ('race_active', '0')
    """)

    cur.execute("""
    INSERT OR IGNORE INTO settings (key, value)
    VALUES ('setup_mode', 'entry')
    """)

    cur.execute("""
    INSERT OR IGNORE INTO settings (key, value)
    VALUES ('current_race_id', '')
    """)

    cur.execute("""
    INSERT OR IGNORE INTO majors (major, label)
    VALUES (1, 'OPEN')
    """)

    conn.commit()
    conn.close()


# =========================
# race control
# =========================

def set_race_active(active):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    INSERT OR REPLACE INTO settings (key, value)
    VALUES ('race_active', ?)
    """, (
        "1" if active else "0",
    ))

    conn.commit()
    conn.close()


def is_race_active():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT value
    FROM settings
    WHERE key = 'race_active'
    """)

    row = cur.fetchone()

    conn.close()

    if row is None:
        return False

    return row["value"] == "1"


# =========================
# laps
# =========================

def save_lap(name, major, lap_number, lap_time):
    conn = get_connection()
    cur = conn.cursor()

    race_id = get_current_race_id()
    now = time.time()

    cur.execute("""
    INSERT INTO laps (
        race_id,
        name,
        major,
        lap_number,
        lap_time,
        timestamp
    )
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        race_id,
        name,
        major,
        lap_number,
        lap_time,
        now
    ))

    conn.commit()
    conn.close()


def clear_laps():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM laps")

    conn.commit()
    conn.close()


# =========================
# devices
# =========================

def get_next_minor():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT MAX(minor)
    FROM devices
    """)

    row = cur.fetchone()

    conn.close()

    if row[0] is None:
        return 1

    return row[0] + 1


def get_device_by_mac(mac_address):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT *
    FROM devices
    WHERE mac_address = ?
    """, (mac_address,))

    row = cur.fetchone()

    conn.close()

    if row is None:
        return None

    return dict(row)


def get_all_devices():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT *
    FROM devices
    ORDER BY minor ASC
    """)

    rows = cur.fetchall()

    conn.close()

    return [dict(r) for r in rows]


def register_device(
    mac_address,
    name="NO_NAME",
    model_name="NO_MODEL",
    team_name="NO_TEAM",
    major=1
):
    existing = get_device_by_mac(mac_address)

    if existing is not None:
        return existing

    minor = get_next_minor()

    now = time.time()

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO devices (
        mac_address,
        minor,
        major,
        name,
        model_name,
        team_name,
        created_at,
        updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        mac_address,
        minor,
        major,
        name,
        model_name,
        team_name,
        now,
        now
    ))

    conn.commit()
    conn.close()

    return get_device_by_mac(mac_address)


def upsert_device(
    mac_address,
    name,
    model_name="",
    team_name="",
    major=1
):
    existing = get_device_by_mac(mac_address)

    now = time.time()

    conn = get_connection()
    cur = conn.cursor()

    if existing is None:
        minor = get_next_minor()

        cur.execute("""
        INSERT INTO devices (
            mac_address,
            minor,
            major,
            name,
            model_name,
            team_name,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mac_address,
            minor,
            major,
            name,
            model_name,
            team_name,
            now,
            now
        ))

    else:
        cur.execute("""
        UPDATE devices
        SET
            major = ?,
            name = ?,
            model_name = ?,
            team_name = ?,
            updated_at = ?
        WHERE mac_address = ?
        """, (
            major,
            name,
            model_name,
            team_name,
            now,
            mac_address
        ))

    conn.commit()
    conn.close()


# =========================
# majors
# =========================

def get_majors():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT *
    FROM majors
    ORDER BY major ASC
    """)

    rows = cur.fetchall()

    conn.close()

    return [dict(r) for r in rows]


def add_or_update_major(major, label):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    INSERT OR REPLACE INTO majors (
        major,
        label
    )
    VALUES (?, ?)
    """, (
        major,
        label
    ))

    conn.commit()
    conn.close()


# =========================
# debug
# =========================

if __name__ == "__main__":
    init_db()
    print("database initialized")

def get_entries_by_major():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        m.major,
        m.label,
        d.minor,
        d.name,
        d.model_name,
        d.team_name,
        d.mac_address
    FROM majors m
    LEFT JOIN devices d
        ON d.major = m.major
    ORDER BY
        m.major ASC,
        d.minor ASC
    """)

    rows = cur.fetchall()
    conn.close()

    result = {}

    for row in rows:
        major = row["major"]

        if major not in result:
            result[major] = {
                "major": major,
                "label": row["label"],
                "entries": []
            }

        if row["mac_address"] is not None:
            result[major]["entries"].append({
                "minor": row["minor"],
                "name": row["name"],
                "model_name": row["model_name"],
                "team_name": row["team_name"],
                "mac_address": row["mac_address"],
            })

    return list(result.values())

def set_setup_mode(mode):
    allowed_modes = ["entry", "locked", "race", "finished"]

    if mode not in allowed_modes:
        raise ValueError("invalid setup_mode")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    INSERT OR REPLACE INTO settings (key, value)
    VALUES ('setup_mode', ?)
    """, (mode,))

    conn.commit()
    conn.close()


def get_setup_mode():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT value
    FROM settings
    WHERE key = 'setup_mode'
    """)

    row = cur.fetchone()
    conn.close()

    if row is None:
        return "entry"

    return row["value"]

def create_new_race():
    conn = get_connection()
    cur = conn.cursor()

    now = time.time()
    race_name = time.strftime("Race %Y-%m-%d %H:%M:%S")

    cur.execute("""
    INSERT INTO races (
        name,
        status,
        started_at,
        finished_at,
        created_at
    )
    VALUES (?, ?, ?, ?, ?)
    """, (
        race_name,
        "race",
        now,
        None,
        now
    ))

    race_id = cur.lastrowid

    cur.execute("""
    INSERT OR REPLACE INTO settings (key, value)
    VALUES ('current_race_id', ?)
    """, (str(race_id),))

    conn.commit()
    conn.close()

    return race_id


def get_current_race_id():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT value
    FROM settings
    WHERE key = 'current_race_id'
    """)

    row = cur.fetchone()
    conn.close()

    if row is None or row["value"] == "":
        return None

    return int(row["value"])


def finish_current_race():
    race_id = get_current_race_id()

    if race_id is None:
        return

    conn = get_connection()
    cur = conn.cursor()

    now = time.time()

    cur.execute("""
    UPDATE races
    SET status = 'finished',
        finished_at = ?
    WHERE id = ?
    """, (now, race_id))

    conn.commit()
    conn.close()


def get_races():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    SELECT *
    FROM races
    ORDER BY id DESC
    """)

    rows = cur.fetchall()
    conn.close()

    return [dict(row) for row in rows]