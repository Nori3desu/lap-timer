import sqlite3
import time
import random

DB_PATH = "/home/earth/lap-timer/lap_timer.db"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

cur.execute("DELETE FROM laps")

base = time.time()

racers = {
    "TARO": 11.8,
    "JIRO": 12.1,
    "HANAKO": 12.4,
    "KEN": 12.7,
    "YUKI": 13.0,
}

rows = []

for name, base_lap in racers.items():
    total = 0

    for lap in range(1, 21):
        if lap == 1:
            lap_time = 0
            total += random.uniform(3, 6)
        else:
            lap_time = round(random.uniform(base_lap - 0.5, base_lap + 0.5), 2)
            total += lap_time

        timestamp = base + total
        rows.append((name, lap, lap_time, timestamp))

print("ROWS TO INSERT:", len(rows))

cur.executemany("""
INSERT INTO laps (
    name,
    lap_number,
    lap_time,
    timestamp
)
VALUES (?, ?, ?, ?)
""", rows)

conn.commit()

cur.execute("SELECT COUNT(*) FROM laps")
count = cur.fetchone()[0]

print("COUNT:", count)

conn.close()
