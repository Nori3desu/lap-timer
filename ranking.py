import sqlite3

DB_FILE = "lap_timer.db"


def get_overall_ranking():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT
        l.name,
        d.minor,
        d.team_name,
        MAX(l.lap_number) AS laps,
        MAX(l.timestamp) AS last_pass_timestamp,
        MIN(NULLIF(l.lap_time, 0)) AS best_lap,
        AVG(NULLIF(l.lap_time, 0)) AS avg_lap
    FROM laps l
    LEFT JOIN devices d ON l.name = d.name
    GROUP BY l.name
    ORDER BY laps DESC, last_pass_timestamp ASC
    """)

    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_major_rankings():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT major, label FROM majors ORDER BY major ASC")
    majors = cur.fetchall()

    result = []

    for major in majors:
        cur.execute("""
        SELECT
            l.name,
            d.minor,
            d.team_name,
            l.major,
            MAX(l.lap_number) AS laps,
            MAX(l.timestamp) AS last_pass_timestamp,
            MIN(NULLIF(l.lap_time, 0)) AS best_lap,
            AVG(NULLIF(l.lap_time, 0)) AS avg_lap
        FROM laps l
        LEFT JOIN devices d ON l.name = d.name
        WHERE l.major = ?
        GROUP BY l.name
        ORDER BY laps DESC, last_pass_timestamp ASC
        """, (major["major"],))

        rows = cur.fetchall()

        result.append({
            "major": major["major"],
            "label": major["label"],
            "ranking": [dict(row) for row in rows],
        })

    conn.close()
    return result


def get_ranking():
    return get_overall_ranking()