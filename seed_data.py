import sqlite3
import time
import random

DB = "lap_timer.db"

conn = sqlite3.connect(DB)
cur = conn.cursor()

cur.execute("DELETE FROM laps")
cur.execute("DELETE FROM devices")
cur.execute("DELETE FROM majors")
cur.execute("DELETE FROM settings")

cur.executemany("""
INSERT INTO majors (major, label)
VALUES (?, ?)
""", [
    (1, "125cc以下"),
    (2, "250cc以上"),
    (3, "Open"),
])

cur.execute("""
INSERT INTO settings (key, value)
VALUES ('race_active', '0')
""")

now = time.time()

devices = [
    ("AA:AA:AA:AA:AA:01", 1, 1, "TARO", "YZ125", "Team A", 72.0),
    ("AA:AA:AA:AA:AA:02", 2, 1, "JIRO", "CRF125", "Team B", 74.0),
    ("AA:AA:AA:AA:AA:03", 3, 1, "SORA", "KX112", "Team C", 73.0),
    ("AA:AA:AA:AA:AA:04", 4, 2, "KEN", "CRF250", "Team D", 68.0),
    ("AA:AA:AA:AA:AA:05", 5, 2, "YUKI", "YZ250F", "Team E", 69.5),
    ("AA:AA:AA:AA:AA:06", 6, 2, "AOI", "RM-Z250", "Team F", 70.0),
    ("AA:AA:AA:AA:AA:07", 7, 3, "HANAKO", "OPEN-X", "Team G", 71.0),
    ("AA:AA:AA:AA:AA:08", 8, 3, "RYO", "KTM350", "Team H", 67.5),
    ("AA:AA:AA:AA:AA:09", 9, 3, "MIO", "FC450", "Team I", 66.8),
    ("AA:AA:AA:AA:AA:10", 10, 3, "NANA", "CRF450R", "Team J", 67.2),
]

cur.executemany("""
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
""", [
    (mac, minor, major, name, model, team, now, now)
    for mac, minor, major, name, model, team, base_lap in devices
])

laps = []

for mac, minor, major, name, model, team, base_lap in devices:
    total = random.uniform(2, 5)

    for lap in range(1, 21):
        if lap == 1:
            lap_time = 0
        else:
            lap_time = round(random.uniform(base_lap - 2.0, base_lap + 2.0), 2)
            total += lap_time

        laps.append((
            name,
            major,
            lap,
            lap_time,
            now + total
        ))

cur.executemany("""
INSERT INTO laps (
    name,
    major,
    lap_number,
    lap_time,
    timestamp
)
VALUES (?, ?, ?, ?, ?)
""", laps)

conn.commit()
conn.close()

print("seed data inserted")
print("devices:", len(devices))
print("laps:", len(laps))