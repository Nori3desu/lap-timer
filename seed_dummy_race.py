import sqlite3
import random
import time

DB_PATH = "lap_timer.db"

classes = [
    (1, "125cc以下"),
    (2, "250cc以下"),
    (3, "Open"),
    (4, "ビギナー"),
]

rider_names = [
    "NORI",
    "TAKA",
    "YUKI",
    "SHO",
    "REN",
    "KAI",
    "HARU",
    "SORA",
    "DAI",
    "KEN",
    "RYO",
    "JUN",
    "NAO",
    "MOTO",
    "AKI",
    "TOMO",
    "KENTA",
    "YUJI",
    "HIDE",
    "MASA",
    "RIN",
    "YUTA",
    "KAZU",
    "REO",
    "TAICHI",
    "KOKI",
    "SHIN",
    "RYOMA",
    "YAMATO",
    "ITSUKI",
    "KANATA",
    "SEIYA",
    "KAITO",
    "MINATO",
    "HAYATO",
    "TATSU",
    "KEISUKE",
    "SOTA",
    "YUUTO",
    "HIRO",
]

machines_by_class = {
    1: [
        "CRF125F",
        "KLX110L",
        "TTR125LW",
        "KX85",
        "YZ85",
    ],

    2: [
        "CRF150R",
        "KX112",
        "YZ125X",
        "KX125",
        "RM125",
    ],

    3: [
        "YZ250F",
        "CRF250R",
        "KX250",
        "RM-Z250",
        "FC250",
    ],

    4: [
        "CRF125F",
        "KLX140R",
        "TTR125",
        "XR100R",
        "SEROW225",
    ],
}

teams = [
    "TEAM SPEED",
    "Garage 46",
    "Blue Racing",
    "Factory Mini",
    "Team KRT",
    "RT Horizon",
    "Moto Friends",
    "TEAM ZERO",
]

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

now = time.time()

# 既存データ削除
cur.execute("DELETE FROM laps")
cur.execute("DELETE FROM devices")
cur.execute("DELETE FROM majors")
cur.execute("DELETE FROM races")

# クラス作成
cur.executemany("""
INSERT INTO majors (major, label)
VALUES (?, ?)
""", classes)

# レース作成
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
    "Dummy Race",
    "race",
    now,
    None,
    now
))

race_id = cur.lastrowid

# settings更新
cur.execute("""
INSERT OR REPLACE INTO settings (key, value)
VALUES ('current_race_id', ?)
""", (str(race_id),))

cur.execute("""
INSERT OR REPLACE INTO settings (key, value)
VALUES ('race_active', '1')
""")

cur.execute("""
INSERT OR REPLACE INTO settings (key, value)
VALUES ('setup_mode', 'race')
""")

driver_index = 1

for major, class_label in classes:

    for class_no in range(1, 11):

        name = rider_names[driver_index - 1]

        model_name = random.choice(
            machines_by_class[major]
        )

        team_name = random.choice(teams)

        minor = driver_index

        mac_address = (
            f"DUMMY:{driver_index:02}:00:00:00:00"
        )

        # devices登録
        cur.execute("""
        INSERT INTO devices (
            mac_address,
            name,
            model_name,
            team_name,
            major,
            minor,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mac_address,
            name,
            model_name,
            team_name,
            major,
            minor,
            now,
            now
        ))

        current_time = (
            now + random.uniform(0, 5)
        )

        for lap in range(1, 21):

            lap_time = random.uniform(
                55.0,
                85.0
            )

            current_time += lap_time

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
                lap,
                lap_time,
                current_time
            ))

        driver_index += 1

conn.commit()
conn.close()

print("dummy data created")
print(f"race_id={race_id}")
print("drivers=40")
print("laps=800")
