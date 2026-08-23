from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import sqlite3
import asyncio
import html
import time
import subprocess
import threading
import os
import socket
import re

from ranking import (
    get_ranking,
    get_overall_ranking,
    get_major_rankings,
)

from database import (
    init_db,
    clear_laps,
    set_race_active,
    is_race_active,
    get_all_devices,
    get_device_by_mac,
    upsert_device,
    get_majors,
    add_or_update_major,
    get_entries_by_major,
    get_setup_mode,
    set_setup_mode,
    create_new_race,
    finish_current_race,
    get_races,
    get_current_race_id,
    get_rssi_settings,
    save_rssi_settings,
    get_latest_rssi_logs,
    get_product_mode,
set_product_mode,
    
)

from ble_device_writer import write_device_info
from ble_entry import scan_and_register_sync
from backup import backup_db

app = FastAPI()

@app.on_event("startup")
def startup():
    init_db()

    product_mode = get_product_mode()

    # LiteはWebサービス起動時に自動計測しない。
    # BLE/RSSI受信サービスは別プロセスで動作を継続する。
    if product_mode == "lite":
        set_race_active(False)
        set_setup_mode("entry")

def format_time(sec):
    if sec is None:
        return "-"

    minutes = int(sec // 60)
    seconds = sec % 60

    return f"{minutes:02}:{seconds:05.2f}"

def format_datetime(ts):
    if ts is None:
        return "-"

    return time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(ts)
    )


def transmitter_name_from_mac(mac_address):
    """
    BLE MACアドレスから送信機名を生成する。
    例: 6B:EB:E2:4D:FE:95 -> TX-4DFE95
    """
    compact_mac = (
        (mac_address or "")
        .replace(":", "")
        .replace("-", "")
        .upper()
    )

    if len(compact_mac) >= 6:
        return "TX-" + compact_mac[-6:]

    return "TX-UNKNOWN"


def get_transmitter_battery_statuses():
    """
    RX別Batteryテーブルから送信機ごとの最新Battery状態を取得する。
    新テーブルがまだ無い場合は従来テーブルへフォールバックする。
    """
    db_path = os.path.join(
        os.path.dirname(__file__),
        "lap_timer.db",
    )

    connection = None

    try:
        connection = sqlite3.connect(
            db_path,
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row

        table_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table'
              AND name='transmitter_battery_receivers'
            """
        ).fetchone()

        if table_exists:
            rows = connection.execute(
                """
                SELECT
                    serial_number,
                    udp_transmitter_id,
                    receiver_id,
                    battery_percent,
                    voltage_mv,
                    updated_at
                FROM transmitter_battery_receivers
                ORDER BY serial_number ASC, receiver_id ASC
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT
                    serial_number,
                    udp_transmitter_id,
                    receiver_id,
                    battery_percent,
                    voltage_mv,
                    updated_at
                FROM transmitter_battery
                ORDER BY serial_number ASC
                """
            ).fetchall()

        grouped = {}

        for row in rows:
            serial_number = row["serial_number"]

            if serial_number not in grouped:
                grouped[serial_number] = {
                    "serial_number": serial_number,
                    "udp_transmitter_id": row["udp_transmitter_id"],
                    "receivers": [],
                }

            grouped[serial_number]["receivers"].append(
                {
                    "receiver_id": row["receiver_id"],
                    "battery_percent": row["battery_percent"],
                    "voltage_mv": row["voltage_mv"],
                    "updated_at": row["updated_at"],
                }
            )

        return list(grouped.values())

    except sqlite3.Error as error:
        print(
            f"[Battery Web] DB read skipped: {error}"
        )
        return []

    finally:
        if connection is not None:
            connection.close()


def get_pi_temperature():
    temp_path = "/sys/class/thermal/thermal_zone0/temp"

    if not os.path.exists(temp_path):
        return None

    with open(temp_path, "r") as f:
        temp_raw = f.read().strip()

    return float(temp_raw) / 1000

def get_lite_summary(race_id):
    summary = {
        "has_start": False,
        "laps": 0,
        "latest_lap": None,
        "best_lap": None,
        "avg_lap": None,
        "lap_history": [],
    }

    if race_id is None:
        return summary

    conn = sqlite3.connect("lap_timer.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    row = cur.execute(
        """
        SELECT
            COUNT(*) AS pass_count,
            MAX(lap_number) AS laps,
            MIN(NULLIF(lap_time, 0)) AS best_lap,
            AVG(NULLIF(lap_time, 0)) AS avg_lap
        FROM laps
        WHERE race_id = ?
        """,
        (race_id,),
    ).fetchone()

    latest = cur.execute(
        """
        SELECT lap_time
        FROM laps
        WHERE race_id = ?
          AND lap_number > 0
          AND lap_time > 0
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (race_id,),
    ).fetchone()

    history = cur.execute(
        """
        SELECT lap_number, lap_time
        FROM laps
        WHERE race_id = ?
          AND lap_number > 0
          AND lap_time > 0
        ORDER BY lap_number DESC
        LIMIT 10
        """,
        (race_id,),
    ).fetchall()

    conn.close()

    if row is not None:
        summary["has_start"] = int(row["pass_count"] or 0) > 0
        summary["laps"] = int(row["laps"] or 0)
        summary["best_lap"] = row["best_lap"]
        summary["avg_lap"] = row["avg_lap"]

    if latest is not None:
        summary["latest_lap"] = latest["lap_time"]

    summary["lap_history"] = [
        {
            "lap_number": int(item["lap_number"]),
            "lap_time": float(item["lap_time"]),
        }
        for item in reversed(history)
    ]

    return summary


def build_lite_lap_history(summary):
    history = summary.get("lap_history", [])

    if not history:
        return ""

    best_lap = summary.get("best_lap")

    rows = []

    for item in history:
        is_best = (
            best_lap is not None
            and abs(item["lap_time"] - best_lap) < 0.001
        )

        best_html = (
            '<span class="best-mark">BEST</span>'
            if is_best
            else ""
        )

        rows.append(
            f"""
            <div class="lap-history-row">
                <span class="lap-number">
                    {item["lap_number"]}周目
                </span>
                <strong>{format_time(item["lap_time"])}</strong>
                {best_html}
            </div>
            """
        )

    return f"""
    <div class="lap-history">
        <div class="lap-history-title">ラップ履歴</div>
        {''.join(rows)}
    </div>
    """


def write_lite_reset_request():
    reset_request_path = os.path.join(
        os.path.dirname(__file__),
        "lite_reset.request",
    )
    with open(reset_request_path, "w", encoding="utf-8") as file:
        file.write(str(time.time()))


@app.get("/", response_class=HTMLResponse)
def root():
    setup_mode = get_setup_mode()
    product_mode = get_product_mode()

    if product_mode != "lite":
        product_mode_labels = {
            "lite": "Lite",
            "rider": "Rider",
            "event": "Event",
            "facility": "Facility",
            "pro": "Pro",
        }
        product_mode_label = product_mode_labels.get(product_mode, product_mode)
        pi_temp = get_pi_temperature()
        pi_temp_text = "-" if pi_temp is None else f"{pi_temp:.1f} ℃"

        return f"""
        <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Lap Timer</title>
        <style>body {{ font-family:sans-serif; padding:20px; text-align:center; }} button {{ width:100%; max-width:320px; font-size:24px; padding:18px; margin-top:20px; }}</style>
        </head><body>
        <h1>Lap Timer</h1>
        <p>製品モード: <strong>{product_mode_label}</strong></p>
        <p>Pi温度: <strong>{pi_temp_text}</strong></p>
        <p><a href="/entry"><button>レースエントリー</button></a></p>
        <p><a href="/entries"><button>エントリーリスト</button></a></p>
        <p><a href="/live"><button>リザルト</button></a></p>
        </body></html>
        """

    current_race_id = get_current_race_id()
    summary = get_lite_summary(current_race_id)
    lap_history_html = build_lite_lap_history(summary)
    active = is_race_active()

    if active:
        lite_state = "running" if summary["has_start"] else "waiting"
    elif setup_mode == "finished":
        lite_state = "finished"
    else:
        lite_state = "stopped"

    refresh_tag = '<meta http-equiv="refresh" content="1">' if lite_state in ("waiting", "running") else ""

    if lite_state == "stopped":
        state_html = """
        <div class="status">⚪ 計測停止中</div>
        <p>「スタート」を押してください。</p>
        <form action="/lite/start" method="post"><button class="start" type="submit">▶ 計測スタート</button></form>
        <p class="sub-link"><a href="/lite/result">過去の計測結果</a></p>
        """
    elif lite_state == "waiting":
        state_html = """
        <div class="status">🟡 最初のゲート通過待ち</div>
        <p>最初にゲートを通過したところからラップ計測を開始します。</p>
        <form action="/lite/stop" method="post"><button class="stop" type="submit">■ 計測ストップ</button></form>
        """
    elif lite_state == "running":
        state_html = f"""
        <div class="status">🟢 ラップ計測中</div>
        <div class="stats">
          <div><span>実周回数</span><strong>{summary['laps']}</strong></div>
          <div><span>最新ラップ</span><strong>{format_time(summary['latest_lap'])}</strong></div>
          <div><span>ベスト</span><strong>{format_time(summary['best_lap'])}</strong></div>
          <div><span>平均</span><strong>{format_time(summary['avg_lap'])}</strong></div>
        </div>
        {lap_history_html}
        <form action="/lite/stop" method="post"><button class="stop" type="submit">■ 計測ストップ</button></form>
        """
    else:
        state_html = f"""
        <div class="status">✅ 計測終了</div>
        <div class="stats">
          <div><span>実周回数</span><strong>{summary['laps']}</strong></div>
          <div><span>ベスト</span><strong>{format_time(summary['best_lap'])}</strong></div>
          <div><span>平均</span><strong>{format_time(summary['avg_lap'])}</strong></div>
        </div>
        {lap_history_html}
        <p><a class="result-button" href="/lite/result">リザルトを見る</a></p>
        <form action="/lite/start" method="post"><button class="start" type="submit">▶ 新しく計測スタート</button></form>
        """

    return f"""
    <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{refresh_tag}
    <title>Lite ラップタイマー</title>
    <style>
    body {{ font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif; margin:0; background:#f4f5f7; color:#222; }}
    .wrap {{ max-width:520px; margin:0 auto; padding:24px 18px 40px; text-align:center; }}
    h1 {{ font-size:28px; margin:12px 0 28px; }}
    .card {{ background:white; border-radius:16px; padding:26px 18px; box-shadow:0 2px 10px rgba(0,0,0,.08); }}
    .status {{ font-size:25px; font-weight:700; margin-bottom:18px; }}
    .stats {{ margin:22px 0 8px; text-align:left; }}
    .stats div {{ display:flex; justify-content:space-between; align-items:baseline; border-bottom:1px solid #eee; padding:12px 4px; gap:16px; }}
    .stats span {{ font-size:18px; }} .stats strong {{ font-size:25px; font-variant-numeric:tabular-nums; }}
    button,.result-button {{ box-sizing:border-box; display:inline-block; width:100%; max-width:360px; border:0; border-radius:12px; padding:17px 14px; margin-top:20px; font-size:22px; font-weight:700; text-decoration:none; cursor:pointer; }}
    button.start {{ background:#1677ff; color:white; }} button.stop {{ background:#333; color:white; }} .result-button {{ background:#e9eef5; color:#222; }}
    .sub-link {{ margin-top:24px; }} .sub-link a {{ color:#444; }}

    .lap-history {{
        margin-top:24px;
        padding:16px;
        background:#f6f6f6;
        border-radius:14px;
        text-align:left;
    }}

    .lap-history-title {{
        font-size:17px;
        font-weight:700;
        margin-bottom:10px;
    }}

    .lap-history-row {{
        display:grid;
        grid-template-columns:70px 1fr 55px;
        align-items:center;
        padding:5px 0;
        font-variant-numeric:tabular-nums;
    }}

    .lap-history-row strong {{
        font-size:18px;
    }}

    .best-mark {{
        font-size:13px;
        font-weight:700;
    }}
    </style></head><body><div class="wrap"><h1>Lite ラップタイマー</h1><div class="card">{state_html}</div></div></body></html>
    """


@app.get("/admin/shutdown", response_class=HTMLResponse)
def shutdown_confirm():
    return """
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>シャットダウン</title>
    </head>
    <body style="font-family:sans-serif;padding:20px;text-align:center;">
        <h1>Piをシャットダウンします</h1>

        <p>ACT LED停止後に電源をOFFしてください。</p>

        <form action="/admin/shutdown/execute" method="post">
            <button style="font-size:28px;padding:20px;width:100%;background:#cc3333;color:white;border:none;">
                シャットダウン実行
            </button>
        </form>

        <p><a href="/admin">戻る</a></p>
    </body>
    </html>
    """

@app.post("/admin/shutdown/execute", response_class=HTMLResponse)
def shutdown_execute():

    backup_db("before_shutdown")

    def delayed_shutdown():
        time.sleep(3)
        subprocess.Popen([
            "sudo",
            "/usr/sbin/shutdown",
            "-h",
            "now"
        ])

    threading.Thread(
        target=delayed_shutdown,
        daemon=True
    ).start()

    return HTMLResponse("""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>
    <body style="font-family:sans-serif;padding:20px;text-align:center;">
        <h1>シャットダウン中</h1>
        <p>約3秒後に安全シャットダウンします。</p>
        <p>ACT LED停止後、電源OFFしてください。</p>
    </body>
    </html>
    """)


@app.get("/ranking")
def ranking():
    return get_ranking()


@app.post("/race/reset")
def reset_race():
    clear_laps()
    set_setup_mode("race")
    set_race_active(True)

    return RedirectResponse(url="/view", status_code=303)

@app.post("/lite/start")
def lite_start():
    product_mode = get_product_mode()
    if product_mode != "lite":
        return RedirectResponse(url="/", status_code=303)

    backup_db("before_lite_start")
    set_race_active(False)

    current_race_id = get_current_race_id()
    if current_race_id is not None:
        finish_current_race()

    create_new_race()
    set_setup_mode("race")
    write_lite_reset_request()
    set_race_active(True)
    backup_db("after_lite_start")
    return RedirectResponse(url="/", status_code=303)


@app.post("/lite/stop")
def lite_stop():
    product_mode = get_product_mode()
    if product_mode != "lite":
        return RedirectResponse(url="/", status_code=303)

    backup_db("before_lite_stop")
    set_race_active(False)
    finish_current_race()
    set_setup_mode("finished")
    backup_db("after_lite_stop")
    return RedirectResponse(url="/", status_code=303)


@app.post("/admin/lite-reset")
def lite_reset():
    return lite_start()


@app.post("/mode/entry")
def mode_entry():
    set_setup_mode("entry")
    set_race_active(False)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/mode/locked")
def mode_locked():
    set_setup_mode("locked")
    set_race_active(False)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/mode/race")
def mode_race():
    backup_db("before_race_start")

    create_new_race()
    set_setup_mode("race")
    set_race_active(True)

    backup_db("after_race_start")

    return RedirectResponse(url="/admin", status_code=303)

@app.post("/mode/finished")
def mode_finished():
    backup_db("before_race_finish")

    set_race_active(False)
    finish_current_race()
    set_setup_mode("finished")

    backup_db("after_race_finish")

    return RedirectResponse(url="/admin", status_code=303)

@app.post("/race/stop")
def stop_race():
    set_setup_mode("finished")
    set_race_active(False)

    return RedirectResponse(url="/view", status_code=303)

def ranking_table(title, rows, major=None, my_minor=None):
    html_text = f"""
    <h2>{html.escape(title)}（{len(rows)}台）</h2>

    <table>
        <tr>
            <th>順位</th>
            <th>ゼッケン</th>
            <th>Name</th>
            <th>車種</th>
            <th>チーム名</th>
            <th>周回数</th>
            <th>Best</th>
            <th>Avg</th>
        </tr>
    """

    if not rows:
        html_text += """
        <tr>
            <td colspan="7">データなし</td>
        </tr>
        """

    for i, row in enumerate(rows, start=1):
        name = row["name"]
        team_name = row["team_name"] or ""
        model_name = row["model_name"] or ""
        minor = row["minor"] if row["minor"] is not None else "-"

        link = f"/driver/{html.escape(name)}"

        if major is not None:
            link += f"?major={major}"

        row_style = ""

        if my_minor is not None and row["minor"] == my_minor:
            row_style = ' style="font-weight:bold;"'

        html_text += f"""
        <tr{row_style}>
            <td>{i}</td>
            <td>{minor}</td>
            <td>
                <a href="{link}">
                    {html.escape(name)}
                </a>
            </td>
            <td>{html.escape(model_name)}</td>
            <td>{html.escape(team_name)}</td>
            <td>{row["laps"]}</td>
            <td>{format_time(row["best_lap"])}</td>
            <td>{format_time(row["avg_lap"])}</td>
        </tr>
        """

    html_text += """
    </table>
    """

    return html_text

def get_overall_ranking_by_race(race_id):
    conn = sqlite3.connect("lap_timer.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT
        l.name,
        d.minor,
        d.model_name,
        d.team_name,
        MAX(l.lap_number) AS laps,
        MAX(l.timestamp) AS last_pass_timestamp,
        MIN(NULLIF(l.lap_time, 0)) AS best_lap,
        AVG(NULLIF(l.lap_time, 0)) AS avg_lap
    FROM laps l
    LEFT JOIN devices d ON l.name = d.name
    WHERE l.race_id = ?
    GROUP BY l.name
    ORDER BY laps DESC, last_pass_timestamp ASC
    """, (race_id,))

    rows = cur.fetchall()
    conn.close()

    return [dict(row) for row in rows]

def get_major_rankings_by_race(race_id):
    conn = sqlite3.connect("lap_timer.db")
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
            d.model_name,
            d.team_name,
            l.major,
            MAX(l.lap_number) AS laps,
            MAX(l.timestamp) AS last_pass_timestamp,
            MIN(NULLIF(l.lap_time, 0)) AS best_lap,
            AVG(NULLIF(l.lap_time, 0)) AS avg_lap
        FROM laps l
        LEFT JOIN devices d ON l.name = d.name
        WHERE l.race_id = ?
          AND l.major = ?
        GROUP BY l.name
        ORDER BY laps DESC, last_pass_timestamp ASC
        """, (race_id, major["major"]))

        rows = cur.fetchall()

        result.append({
            "major": major["major"],
            "label": major["label"],
            "ranking": [dict(row) for row in rows],
        })

    conn.close()
    return result


def get_lite_race_history():
    conn = get_connection()
    cur = conn.cursor()

    rows = cur.execute(
        """
        SELECT
            r.id,
            r.name,
            r.started_at,
            r.finished_at,
            COUNT(
                CASE
                    WHEN l.lap_number > 0 AND l.lap_time > 0
                    THEN 1
                END
            ) AS laps,
            MIN(
                CASE
                    WHEN l.lap_number > 0 AND l.lap_time > 0
                    THEN l.lap_time
                END
            ) AS best_lap,
            AVG(
                CASE
                    WHEN l.lap_number > 0 AND l.lap_time > 0
                    THEN l.lap_time
                END
            ) AS avg_lap
        FROM races r
        LEFT JOIN laps l
            ON l.race_id = r.id
        GROUP BY
            r.id,
            r.name,
            r.started_at,
            r.finished_at
        ORDER BY r.id DESC
        """
    ).fetchall()

    conn.close()

    return [dict(row) for row in rows]


def get_lite_race_detail(race_id):
    conn = get_connection()
    cur = conn.cursor()

    race = cur.execute(
        """
        SELECT
            id,
            name,
            started_at,
            finished_at
        FROM races
        WHERE id = ?
        """,
        (race_id,),
    ).fetchone()

    if race is None:
        conn.close()
        return None

    summary = cur.execute(
        """
        SELECT
            COUNT(*) AS laps,
            MIN(lap_time) AS best_lap,
            AVG(lap_time) AS avg_lap
        FROM laps
        WHERE race_id = ?
          AND lap_number > 0
          AND lap_time > 0
        """,
        (race_id,),
    ).fetchone()

    laps = cur.execute(
        """
        SELECT
            lap_number,
            lap_time,
            timestamp
        FROM laps
        WHERE race_id = ?
          AND lap_number > 0
          AND lap_time > 0
        ORDER BY lap_number ASC
        """,
        (race_id,),
    ).fetchall()

    conn.close()

    return {
        "race": dict(race),
        "summary": dict(summary),
        "laps": [dict(row) for row in laps],
    }


def format_lite_datetime(value):
    if value is None:
        return "-"

    try:
        return time.strftime(
            "%Y年%m月%d日 %H:%M",
            time.localtime(float(value)),
        )
    except Exception:
        return "-"


@app.get("/lite/result", response_class=HTMLResponse)
def lite_result():
    races = get_lite_race_history()

    html_text = """
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport"
              content="width=device-width, initial-scale=1">

        <title>過去の計測結果</title>

        <style>
            body {
                font-family: sans-serif;
                max-width: 520px;
                margin: 0 auto;
                padding: 18px;
                background: #ffffff;
                color: #111;
            }

            h1 {
                text-align: center;
                font-size: 26px;
                margin-bottom: 24px;
            }

            .back {
                display: inline-block;
                margin-bottom: 14px;
                color: #333;
            }

            .race-card {
                background: #f5f5f5;
                border-radius: 14px;
                padding: 18px;
                margin-bottom: 16px;
            }

            .date {
                font-size: 19px;
                font-weight: 700;
                margin-bottom: 12px;
            }

            .summary-row {
                display: flex;
                justify-content: space-between;
                padding: 5px 0;
                font-size: 17px;
            }

            .detail-button {
                display: block;
                margin-top: 15px;
                padding: 13px;
                border-radius: 10px;
                background: #1677ff;
                color: white;
                text-align: center;
                text-decoration: none;
                font-size: 18px;
                font-weight: 700;
            }

            .empty {
                background: #f5f5f5;
                border-radius: 14px;
                padding: 24px;
                text-align: center;
            }
        </style>
    </head>

    <body>
        <a class="back" href="/">← Liteに戻る</a>

        <h1>過去の計測結果</h1>
    """

    visible_races = [
        race for race in races
        if int(race["laps"] or 0) > 0
    ]

    if not visible_races:
        html_text += """
        <div class="empty">
            まだ保存された計測結果がありません。
        </div>
        """

    else:
        for race in visible_races:
            html_text += f"""
            <div class="race-card">

                <div class="date">
                    {format_lite_datetime(race["started_at"])}
                </div>

                <div class="summary-row">
                    <span>実周回数</span>
                    <strong>{int(race["laps"] or 0)}</strong>
                </div>

                <div class="summary-row">
                    <span>BEST</span>
                    <strong>{format_time(race["best_lap"])}</strong>
                </div>

                <div class="summary-row">
                    <span>平均</span>
                    <strong>{format_time(race["avg_lap"])}</strong>
                </div>

                <a class="detail-button"
                   href="/lite/result/{race["id"]}">
                    詳細を見る
                </a>

            </div>
            """

    html_text += """
    </body>
    </html>
    """

    return html_text


@app.get(
    "/lite/result/{race_id}",
    response_class=HTMLResponse,
)
def lite_result_detail(race_id: int):
    detail = get_lite_race_detail(race_id)

    if detail is None:
        return """
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport"
                  content="width=device-width, initial-scale=1">
        </head>
        <body>
            <p>指定された計測結果が見つかりません。</p>
            <p><a href="/lite/result">← 過去の計測結果へ戻る</a></p>
        </body>
        </html>
        """

    race = detail["race"]
    summary = detail["summary"]
    laps = detail["laps"]

    best_lap = summary["best_lap"]

    lap_rows = ""

    for lap in laps:
        is_best = (
            best_lap is not None
            and abs(float(lap["lap_time"]) - float(best_lap)) < 0.001
        )

        best_html = (
            '<span class="best-mark">BEST</span>'
            if is_best
            else ""
        )

        lap_rows += f"""
        <div class="lap-row">
            <span>{lap["lap_number"]}周目</span>
            <strong>{format_time(lap["lap_time"])}</strong>
            {best_html}
        </div>
        """

    html_text = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport"
              content="width=device-width, initial-scale=1">

        <title>Lite 計測詳細</title>

        <style>
            body {{
                font-family: sans-serif;
                max-width: 520px;
                margin: 0 auto;
                padding: 18px;
                color: #111;
            }}

            h1 {{
                text-align: center;
                font-size: 25px;
            }}

            .date {{
                text-align: center;
                font-size: 18px;
                margin-bottom: 20px;
            }}

            .summary {{
                background: #f5f5f5;
                padding: 17px;
                border-radius: 14px;
            }}

            .summary-row {{
                display: flex;
                justify-content: space-between;
                padding: 6px 0;
                font-size: 18px;
            }}

            .history {{
                margin-top: 22px;
                background: #f5f5f5;
                padding: 17px;
                border-radius: 14px;
            }}

            .history-title {{
                font-size: 19px;
                font-weight: 700;
                margin-bottom: 10px;
            }}

            .lap-row {{
                display: grid;
                grid-template-columns: 75px 1fr 55px;
                align-items: center;
                padding: 6px 0;
                font-variant-numeric: tabular-nums;
            }}

            .lap-row strong {{
                font-size: 18px;
            }}

            .best-mark {{
                font-size: 13px;
                font-weight: 700;
            }}

            .back {{
                display: inline-block;
                margin-bottom: 14px;
                color: #333;
            }}
        </style>
    </head>

    <body>

        <a class="back" href="/lite/result">
            ← 過去の計測結果へ戻る
        </a>

        <h1>計測結果</h1>

        <div class="date">
            {format_lite_datetime(race["started_at"])}
        </div>

        <div class="summary">

            <div class="summary-row">
                <span>実周回数</span>
                <strong>{int(summary["laps"] or 0)}</strong>
            </div>

            <div class="summary-row">
                <span>BEST</span>
                <strong>{format_time(summary["best_lap"])}</strong>
            </div>

            <div class="summary-row">
                <span>平均</span>
                <strong>{format_time(summary["avg_lap"])}</strong>
            </div>

        </div>

        <div class="history">
            <div class="history-title">
                ラップ履歴
            </div>

            {lap_rows}
        </div>

    </body>
    </html>
    """

    return html_text


@app.get("/live", response_class=HTMLResponse)
def view(my_minor: int | None = None):
    current_race_id = get_current_race_id()

    if current_race_id is None:
        overall_rows = []
        major_rankings = []
    else:
        overall_rows = get_overall_ranking_by_race(current_race_id)
        major_rankings = get_major_rankings_by_race(current_race_id)

    setup_mode = get_setup_mode()

    mode_labels = {
        "entry": "エントリー受付中",
        "locked": "エントリー締切",
        "race": "レース中",
        "finished": "レース終了",
    }

    mode_label = mode_labels.get(setup_mode, setup_mode)

    refresh_tag = ""

    if setup_mode == "race":
        refresh_tag = '<meta http-equiv="refresh" content="2">'

    html_text = f"""

    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Lap Timer</title>

        {refresh_tag}

        <style>
            body {{
                font-family: sans-serif;
                padding: 16px;
            }}

            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 8px;
                margin-bottom: 28px;
            }}

            th, td {{
                border-bottom: 1px solid #ccc;
                padding: 8px;
                text-align: left;
            }}

            th {{
                background: #eee;
            }}

            a {{
                text-decoration: none;
            }}

            button {{
                font-size: 16px;
                padding: 10px 16px;
                margin-right: 8px;
            }}

            h2 {{
                margin-top: 28px;
            }}
        </style>
    </head>

    <body>
        <h1>Lap Timer</h1>
        
        <p>
            現在の状態: <strong>{mode_label}</strong>
        </p>

        <p>

        </p>

    """

    html_text += ranking_table("総合順位",overall_rows,my_minor=my_minor)

    for group in major_rankings:
        html_text += ranking_table(
            group["label"],
            group["ranking"],
            group["major"],
            my_minor=my_minor
        )

    html_text += """
    </body>
    </html>
    """

    return html_text



# ============================================================
# レシーバー リモート管理
# ============================================================

RECEIVER_CONTROL_PORT = 5101
RECEIVER_IP_PREFIX = "192.168.4."


def receiver_ip_from_id(receiver_id: str):
    """
    RX-0001 -> 192.168.4.101
    RX-0002 -> 192.168.4.102
    RX-0003 -> 192.168.4.103
    """
    match = re.fullmatch(
        r"RX-(\d{4})",
        receiver_id,
    )

    if match is None:
        return None

    number = int(match.group(1))

    host = 100 + number

    if host < 1 or host > 254:
        return None

    return (
        RECEIVER_IP_PREFIX
        + str(host)
    )


def get_known_receiver_ids():
    """
    DBに記録されたreceiver_idから
    管理対象RXを自動取得する。
    """

    db_path = os.path.join(
        os.path.dirname(__file__),
        "lap_timer.db",
    )

    receiver_ids = set()

    conn = sqlite3.connect(
        db_path,
        timeout=5.0,
    )

    try:
        cur = conn.cursor()

        # RSSIログ
        try:
            cur.execute(
                """
                SELECT DISTINCT receiver_id
                FROM rssi_logs
                WHERE receiver_id IS NOT NULL
                """
            )

            for row in cur.fetchall():
                receiver_id = str(row[0])

                if receiver_ip_from_id(
                    receiver_id
                ) is not None:
                    receiver_ids.add(
                        receiver_id
                    )

        except sqlite3.OperationalError:
            pass

        # バッテリー受信履歴
        try:
            cur.execute(
                """
                SELECT DISTINCT receiver_id
                FROM transmitter_battery_receivers
                WHERE receiver_id IS NOT NULL
                """
            )

            for row in cur.fetchall():
                receiver_id = str(row[0])

                if receiver_ip_from_id(
                    receiver_id
                ) is not None:
                    receiver_ids.add(
                        receiver_id
                    )

        except sqlite3.OperationalError:
            pass

    finally:
        conn.close()

    return sorted(receiver_ids)


def send_receiver_control(
    receiver_id: str,
    command: str,
    timeout: float = 1.5,
):
    ip_address = receiver_ip_from_id(
        receiver_id
    )

    if ip_address is None:
        return {
            "ok": False,
            "receiver_id": receiver_id,
            "ip": None,
            "message": "RX ID形式エラー",
        }

    message = (
        f"{command},{receiver_id}"
    ).encode("utf-8")

    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM,
    )

    sock.settimeout(timeout)

    try:
        sock.sendto(
            message,
            (
                ip_address,
                RECEIVER_CONTROL_PORT,
            ),
        )

        data, address = sock.recvfrom(
            1024
        )

        response = data.decode(
            "utf-8",
            errors="replace",
        ).strip()

        expected = (
            f"ACK,{command},{receiver_id}"
        )

        return {
            "ok": response == expected,
            "receiver_id": receiver_id,
            "ip": ip_address,
            "message": response,
        }

    except socket.timeout:
        return {
            "ok": False,
            "receiver_id": receiver_id,
            "ip": ip_address,
            "message": "応答なし",
        }

    except OSError as exc:
        return {
            "ok": False,
            "receiver_id": receiver_id,
            "ip": ip_address,
            "message": str(exc),
        }

    finally:
        sock.close()


def receiver_control_result_page(
    title: str,
    results,
):
    rows = ""

    for result in results:
        receiver_id = html.escape(
            str(result["receiver_id"])
        )

        ip_address = html.escape(
            str(result.get("ip") or "-")
        )

        message = html.escape(
            str(result.get("message") or "")
        )

        if result["ok"]:
            status = "🟢 成功"
        else:
            status = "🔴 失敗"

        rows += f"""
        <div style="
            background:#fff;
            border:1px solid #ddd;
            border-radius:10px;
            padding:14px;
            margin-bottom:10px;
        ">
            <div style="font-size:1.15em;font-weight:800;">
                {receiver_id}
            </div>
            <div>IP：{ip_address}</div>
            <div>{status}</div>
            <div style="font-size:0.9em;color:#555;">
                {message}
            </div>
        </div>
        """

    return HTMLResponse(
        f"""
        <!DOCTYPE html>
        <html lang="ja">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport"
                  content="width=device-width, initial-scale=1">
            <title>{html.escape(title)}</title>
        </head>
        <body style="
            font-family:-apple-system,BlinkMacSystemFont,
            'Segoe UI',sans-serif;
            max-width:720px;
            margin:0 auto;
            padding:20px;
            background:#f5f5f5;
        ">
            <h1>{html.escape(title)}</h1>

            {rows}

            <a href="/admin/receivers"
               style="
                   display:block;
                   text-align:center;
                   padding:14px;
                   background:#333;
                   color:#fff;
                   text-decoration:none;
                   border-radius:10px;
                   margin-top:18px;
               ">
                レシーバー管理へ戻る
            </a>
        </body>
        </html>
        """
    )


@app.get(
    "/admin/receivers",
    response_class=HTMLResponse,
)
def receiver_admin():
    receiver_ids = (
        get_known_receiver_ids()
    )

    cards = ""

    for receiver_id in receiver_ids:
        ip_address = (
            receiver_ip_from_id(
                receiver_id
            )
        )

        ping_result = (
            send_receiver_control(
                receiver_id,
                "PING",
                timeout=0.6,
            )
        )

        if ping_result["ok"]:
            status_text = "🟢 ONLINE"
            status_color = "#198754"
        else:
            status_text = "🔴 応答なし"
            status_color = "#dc3545"

        safe_receiver_id = html.escape(
            receiver_id
        )

        safe_ip = html.escape(
            str(ip_address)
        )

        cards += f"""
        <div style="
            background:#fff;
            border:1px solid #ddd;
            border-radius:12px;
            padding:16px;
            margin-bottom:14px;
        ">
            <div style="
                font-size:1.25em;
                font-weight:800;
            ">
                {safe_receiver_id}
            </div>

            <div style="margin-top:5px;">
                IP：{safe_ip}
            </div>

            <div style="
                margin-top:5px;
                font-weight:800;
                color:{status_color};
            ">
                {status_text}
            </div>

            <form
                method="post"
                action="/admin/receivers/restart"
                style="margin-top:12px;"
                onsubmit="return confirm(
                    '{safe_receiver_id} を再起動しますか？'
                );"
            >
                <input
                    type="hidden"
                    name="receiver_id"
                    value="{safe_receiver_id}"
                >

                <button
                    type="submit"
                    style="
                        width:100%;
                        padding:12px;
                        font-size:1em;
                        font-weight:700;
                    "
                >
                    {safe_receiver_id} を再起動
                </button>
            </form>
        </div>
        """

    if not cards:
        cards = """
        <div style="
            background:#fff3cd;
            padding:15px;
            border-radius:10px;
        ">
            管理対象レシーバーが
            DBから見つかりません。
        </div>
        """

    return HTMLResponse(
        f"""
        <!DOCTYPE html>
        <html lang="ja">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport"
                  content="width=device-width, initial-scale=1">
            <title>レシーバー管理</title>
        </head>

        <body style="
            font-family:-apple-system,BlinkMacSystemFont,
            'Segoe UI',sans-serif;
            max-width:720px;
            margin:0 auto;
            padding:20px;
            background:#f5f5f5;
        ">

            <h1>レシーバー管理</h1>

            <div style="
                background:#e7f1ff;
                border:1px solid #9ec5fe;
                border-radius:10px;
                padding:14px;
                margin-bottom:18px;
            ">
                コース検証・本番計測開始前に
                レシーバーを再起動できます。
                <br>
                RX台数はDBから自動取得します。
            </div>

            {cards}

            <form
                method="post"
                action="/admin/receivers/restart-all"
                onsubmit="return confirm(
                    '全レシーバーを再起動しますか？'
                );"
            >
                <button
                    type="submit"
                    style="
                        width:100%;
                        padding:15px;
                        margin-top:10px;
                        font-size:1.05em;
                        font-weight:800;
                        background:#dc3545;
                        color:#fff;
                        border:0;
                        border-radius:10px;
                    "
                >
                    全レシーバーを再起動
                </button>
            </form>

            <a href="/admin"
               style="
                   display:block;
                   text-align:center;
                   padding:14px;
                   margin-top:20px;
                   background:#333;
                   color:#fff;
                   text-decoration:none;
                   border-radius:10px;
               ">
                管理者メニューへ戻る
            </a>

        </body>
        </html>
        """
    )


@app.post(
    "/admin/receivers/restart",
    response_class=HTMLResponse,
)
def restart_receiver(
    receiver_id: str = Form(...),
):
    known_receivers = (
        get_known_receiver_ids()
    )

    if receiver_id not in known_receivers:
        return receiver_control_result_page(
            "レシーバー再起動",
            [
                {
                    "ok": False,
                    "receiver_id":
                        receiver_id,
                    "ip": None,
                    "message":
                        "未登録RXです",
                }
            ],
        )

    restart_result = (
        send_receiver_control(
            receiver_id,
            "RESTART",
            timeout=2.0,
        )
    )

    # ACK受信後、RXの再起動と
    # Wi-Fi復帰を待つ。
    if restart_result["ok"]:
        time.sleep(5.0)

        recovered = False
        last_ping = None

        for _ in range(8):
            last_ping = (
                send_receiver_control(
                    receiver_id,
                    "PING",
                    timeout=0.8,
                )
            )

            if last_ping["ok"]:
                recovered = True
                break

            time.sleep(1.0)

        if recovered:
            restart_result[
                "message"
            ] = (
                "再起動ACK受信 / "
                "PING復帰確認OK"
            )
        else:
            restart_result["ok"] = False
            restart_result[
                "message"
            ] = (
                "再起動ACK受信 / "
                "PING復帰確認できず"
            )

    return receiver_control_result_page(
        "レシーバー再起動結果",
        [restart_result],
    )


@app.post(
    "/admin/receivers/restart-all",
    response_class=HTMLResponse,
)
def restart_all_receivers():
    receiver_ids = (
        get_known_receiver_ids()
    )

    restart_results = []

    # まず全RXへRESTARTを送る。
    for receiver_id in receiver_ids:
        restart_results.append(
            send_receiver_control(
                receiver_id,
                "RESTART",
                timeout=2.0,
            )
        )

    # 再起動待ち
    time.sleep(5.0)

    final_results = []

    # 各RXの復帰を確認する。
    for restart_result in restart_results:
        receiver_id = (
            restart_result[
                "receiver_id"
            ]
        )

        if not restart_result["ok"]:
            final_results.append(
                restart_result
            )
            continue

        recovered = False

        for _ in range(8):
            ping_result = (
                send_receiver_control(
                    receiver_id,
                    "PING",
                    timeout=0.8,
                )
            )

            if ping_result["ok"]:
                recovered = True
                break

            time.sleep(1.0)

        if recovered:
            restart_result[
                "message"
            ] = (
                "再起動ACK受信 / "
                "PING復帰確認OK"
            )
        else:
            restart_result["ok"] = False
            restart_result[
                "message"
            ] = (
                "再起動ACK受信 / "
                "PING復帰確認できず"
            )

        final_results.append(
            restart_result
        )

    return receiver_control_result_page(
        "全レシーバー再起動結果",
        final_results,
    )





# ============================================================
# ゲート診断
# ============================================================

GATE_DIAGNOSTIC_EVENT_LABELS = {
    "RESET_WAITING_CLEAR":
        ("🟠", "リセット後クリア待ち", "event-warning"),

    "MEASUREMENT_READY":
        ("🟢", "計測準備OK", "event-good"),

    "ENTRY_START":
        ("🔵", "ENTRY開始", "event-info"),

    "ENTRY_CANCEL":
        ("⚪", "ENTRYキャンセル", "event-muted"),

    "ENTRY_CONFIRMED":
        ("✅", "ENTRY成立", "event-good"),

    "LAP_RECORDED":
        ("🏁", "ラップ記録", "event-lap"),

    "LAP_BLOCKED_MIN_TIME":
        ("⛔", "最短ラップ時間で除外", "event-danger"),

    "EXIT_START":
        ("🟡", "EXIT開始", "event-warning"),

    "EXIT_CANCEL":
        ("↩️", "EXITキャンセル", "event-warning"),

    "EXIT_CONFIRMED":
        ("✅", "EXIT成立", "event-good"),
}


def parse_gate_detail(detail: str):
    result = {}

    if not detail:
        return result

    patterns = {
        "elapsed":
            r"\belapsed=([0-9.]+)",

        "remaining":
            r"\bremaining=([0-9.]+)",

        "minimum":
            r"\bminimum=([0-9.]+)",

        "combined":
            r"\bcombined=([^\s]+)",

        "fresh_rx":
            r"\bfresh_rx=([0-9]+)",

        "diff":
            r"\bdiff=([^\s]+)",

        "lap":
            r"\blap=([0-9]+)",

        "lap_time":
            r"\blap_time=([^\s]+)",
    }

    for key, pattern in patterns.items():
        match = re.search(
            pattern,
            detail,
        )

        if match:
            result[key] = match.group(1)

    rx_values = {}

    for match in re.finditer(
        r"'(RX-[0-9]+)':\s*(-?[0-9]+)",
        detail,
    ):
        rx_values[
            match.group(1)
        ] = int(
            match.group(2)
        )

    result["rx_values"] = rx_values

    return result


def gate_event_detail_japanese(
    event_type: str,
    detail: str,
):
    parsed = parse_gate_detail(
        detail
    )

    rx_values = parsed.get(
        "rx_values",
        {},
    )

    rx_text = ""

    if rx_values:
        rx_text = " / ".join(
            f"{receiver_id} {rssi} dBm"
            for receiver_id, rssi
            in sorted(
                rx_values.items()
            )
        )

    combined = parsed.get(
        "combined"
    )

    if event_type == "ENTRY_START":
        parts = [
            "ENTRY判定を開始しました。"
        ]

        if rx_text:
            parts.append(
                f"受信：{rx_text}"
            )

        if combined not in (
            None,
            "None",
        ):
            parts.append(
                f"統合RSSI {combined} dBm"
            )

        return "<br>".join(parts)

    if event_type == "ENTRY_CANCEL":
        elapsed = parsed.get(
            "elapsed"
        )

        parts = [
            "ENTRY条件を維持できなかったため取消しました。"
        ]

        if elapsed is not None:
            parts.append(
                f"継続時間 {float(elapsed):.3f} 秒"
            )

        if rx_text:
            parts.append(
                f"受信：{rx_text}"
            )

        return "<br>".join(parts)

    if event_type == "ENTRY_CONFIRMED":
        elapsed = parsed.get(
            "elapsed"
        )

        parts = [
            "ENTRY成立。ゲート通過として認識しました。"
        ]

        if elapsed is not None:
            parts.append(
                f"ENTRY継続 {float(elapsed):.3f} 秒"
            )

        if rx_text:
            parts.append(
                f"受信：{rx_text}"
            )

        if combined not in (
            None,
            "None",
        ):
            parts.append(
                f"統合RSSI {combined} dBm"
            )

        return "<br>".join(parts)

    if event_type == "LAP_RECORDED":
        lap = parsed.get(
            "lap"
        )

        lap_time = parsed.get(
            "lap_time"
        )

        parts = [
            "周回を記録しました。"
        ]

        if lap:
            parts.append(
                f"ラップ番号 {lap}"
            )

        if (
            lap_time
            and lap_time != "FIRST"
        ):
            try:
                parts.append(
                    f"ラップタイム {float(lap_time):.3f} 秒"
                )
            except ValueError:
                pass

        return "<br>".join(parts)

    if event_type == "LAP_BLOCKED_MIN_TIME":
        elapsed = parsed.get(
            "elapsed"
        )

        remaining = parsed.get(
            "remaining"
        )

        minimum = parsed.get(
            "minimum"
        )

        parts = [
            "ENTRYは成立しましたが、周回には加算しませんでした。"
        ]

        if elapsed is not None:
            parts.append(
                f"前回ラップから {float(elapsed):.3f} 秒"
            )

        if minimum is not None:
            parts.append(
                f"最短ラップ設定 {float(minimum):.3f} 秒"
            )

        if remaining is not None:
            parts.append(
                f"あと {float(remaining):.3f} 秒不足"
            )

        return "<br>".join(parts)

    if event_type == "EXIT_START":
        if rx_values:
            return (
                "EXIT判定を開始しました。"
                f"<br>受信：{rx_text}"
            )

        return (
            "RXの有効信号がなくなり、"
            "EXIT判定を開始しました。"
        )

    if event_type == "EXIT_CANCEL":
        parts = [
            "再び信号を受信したため、EXIT判定を取消しました。"
        ]

        if rx_text:
            parts.append(
                f"受信：{rx_text}"
            )

        return "<br>".join(parts)

    if event_type == "EXIT_CONFIRMED":
        return (
            "EXIT成立。ゲートから十分離れました。"
            "<br>次の通過を受け付けられます。"
        )

    if event_type == "RESET_WAITING_CLEAR":
        return (
            "リセット後のクリア待ちです。"
            "<br>TXをゲートから十分離してください。"
        )

    if event_type == "MEASUREMENT_READY":
        return (
            "リセット後のクリア確認が完了しました。"
            "<br>次の通過から計測できます。"
        )

    return html.escape(
        detail
    )


def get_current_gate_status(events):
    if not events:
        return (
            "⚪",
            "状態不明",
            "診断イベントがありません。",
            "#f3f4f6",
            "#6c757d",
        )

    latest = events[0]

    event_type = str(
        latest.get(
            "event_type",
            "",
        )
    )

    if event_type == "RESET_WAITING_CLEAR":
        return (
            "🔴",
            "リセット後クリア待ち",
            "TXをゲートから十分離してください。",
            "#fdecec",
            "#dc3545",
        )

    if event_type in (
        "ENTRY_START",
    ):
        return (
            "🟡",
            "ENTRY判定中",
            "現在、ENTRY条件の継続を確認しています。",
            "#fff7db",
            "#d39e00",
        )

    if event_type in (
        "ENTRY_CONFIRMED",
        "LAP_RECORDED",
        "LAP_BLOCKED_MIN_TIME",
        "EXIT_START",
        "EXIT_CANCEL",
    ):
        return (
            "🔵",
            "ゲート内・EXIT待ち",
            "次の周回を受け付けるにはEXIT成立が必要です。",
            "#e8f1ff",
            "#0d6efd",
        )

    if event_type in (
        "MEASUREMENT_READY",
        "ENTRY_CANCEL",
        "EXIT_CONFIRMED",
    ):
        return (
            "🟢",
            "計測可能",
            "次の通過を受け付けられます。",
            "#e7f6ed",
            "#198754",
        )

    return (
        "⚪",
        "状態確認中",
        f"最新イベント：{html.escape(event_type)}",
        "#f3f4f6",
        "#6c757d",
    )


def get_gate_diagnostic_events(
    minutes: int = 20,
    limit: int = 300,
):
    minutes = max(
        1,
        min(int(minutes), 180),
    )

    limit = max(
        1,
        min(int(limit), 1000),
    )

    db_path = os.path.join(
        os.path.dirname(__file__),
        "lap_timer.db",
    )

    since = (
        time.time()
        - (minutes * 60)
    )

    conn = sqlite3.connect(
        db_path,
        timeout=5.0,
    )

    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                id,
                serial_number,
                event_type,
                timestamp,
                detail
            FROM gate_state_events
            WHERE timestamp >= ?
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            (
                since,
                limit,
            ),
        )

        return [
            dict(row)
            for row in cur.fetchall()
        ]

    except sqlite3.OperationalError:
        return []

    finally:
        conn.close()


@app.get(
    "/admin/gate-diagnostics",
    response_class=HTMLResponse,
)
def gate_diagnostics(
    minutes: int = 20,
):
    minutes = max(
        1,
        min(int(minutes), 180),
    )

    events = get_gate_diagnostic_events(
        minutes=minutes,
        limit=500,
    )

    (
        current_icon,
        current_title,
        current_message,
        current_background,
        current_border,
    ) = get_current_gate_status(
        events
    )

    # --------------------------------------------------------
    # 簡易集計
    # --------------------------------------------------------

    event_counts = {}

    for event in events:
        event_type = event[
            "event_type"
        ]

        event_counts[event_type] = (
            event_counts.get(
                event_type,
                0,
            )
            + 1
        )

    lap_count = event_counts.get(
        "LAP_RECORDED",
        0,
    )

    blocked_count = event_counts.get(
        "LAP_BLOCKED_MIN_TIME",
        0,
    )

    entry_cancel_count = event_counts.get(
        "ENTRY_CANCEL",
        0,
    )

    exit_cancel_count = event_counts.get(
        "EXIT_CANCEL",
        0,
    )

    # --------------------------------------------------------
    # イベントカード
    # --------------------------------------------------------

    cards = ""

    for event in events:
        event_type = str(
            event["event_type"]
        )

        timestamp = float(
            event["timestamp"]
        )

        serial_number = html.escape(
            str(
                event["serial_number"]
            )
        )

        raw_detail = str(
            event.get("detail")
            or ""
        )

        detail = gate_event_detail_japanese(
            event_type,
            raw_detail,
        )

        icon, label, css_class = (
            GATE_DIAGNOSTIC_EVENT_LABELS.get(
                event_type,
                (
                    "⚪",
                    event_type,
                    "event-muted",
                ),
            )
        )

        time_text = time.strftime(
            "%H:%M:%S",
            time.localtime(timestamp),
        )

        milliseconds = int(
            (
                timestamp
                - int(timestamp)
            )
            * 1000
        )

        time_text = (
            f"{time_text}."
            f"{milliseconds:03d}"
        )

        cards += f"""
        <div class="event-card {css_class}">
            <div class="event-top">
                <div class="event-title">
                    {icon} {html.escape(label)}
                </div>

                <div class="event-time">
                    {time_text}
                </div>
            </div>

            <div class="event-tx">
                {serial_number}
            </div>

            {
                f'<div class="event-detail">{detail}</div>'
                if detail
                else ''
            }
        </div>
        """

    if not cards:
        cards = """
        <div class="empty">
            この時間帯の診断イベントはありません。
        </div>
        """

    return HTMLResponse(
        f"""
        <!DOCTYPE html>
        <html lang="ja">
        <head>
            <meta charset="UTF-8">

            <meta
                name="viewport"
                content="width=device-width, initial-scale=1"
            >

            <meta
                http-equiv="refresh"
                content="5"
            >

            <title>ゲート診断</title>

            <style>
                * {{
                    box-sizing: border-box;
                }}

                body {{
                    margin: 0;
                    padding: 14px;
                    background: #f4f5f7;
                    font-family:
                        -apple-system,
                        BlinkMacSystemFont,
                        "Segoe UI",
                        sans-serif;
                    color: #222;
                }}

                .page {{
                    max-width: 760px;
                    margin: 0 auto;
                }}

                h1 {{
                    margin:
                        6px 0 8px 0;
                    font-size: 26px;
                }}

                .note {{
                    background: #fff;
                    border-radius: 12px;
                    padding: 12px;
                    margin-bottom: 12px;
                    font-size: 14px;
                    line-height: 1.5;
                }}

                .current-status {{
                    border-radius: 14px;
                    padding: 15px;
                    margin-bottom: 12px;
                    border: 2px solid;
                }}

                .current-status-title {{
                    font-size: 22px;
                    font-weight: 850;
                }}

                .current-status-message {{
                    margin-top: 6px;
                    font-size: 15px;
                    line-height: 1.45;
                }}

                .summary {{
                    display: grid;
                    grid-template-columns:
                        repeat(2, 1fr);
                    gap: 8px;
                    margin-bottom: 12px;
                }}

                .summary-card {{
                    background: #fff;
                    border-radius: 10px;
                    padding: 11px;
                    text-align: center;
                    border: 1px solid #ddd;
                }}

                .summary-number {{
                    font-size: 24px;
                    font-weight: 800;
                }}

                .summary-label {{
                    margin-top: 3px;
                    font-size: 12px;
                    color: #555;
                }}

                .range-buttons {{
                    display: grid;
                    grid-template-columns:
                        repeat(3, 1fr);
                    gap: 7px;
                    margin-bottom: 14px;
                }}

                .range-buttons a {{
                    text-align: center;
                    text-decoration: none;
                    color: #222;
                    background: #fff;
                    border: 1px solid #bbb;
                    border-radius: 9px;
                    padding: 10px 4px;
                    font-weight: 700;
                }}

                .event-card {{
                    background: #fff;
                    border-radius: 12px;
                    padding: 12px 13px;
                    margin-bottom: 8px;
                    border-left:
                        6px solid #aaa;
                    box-shadow:
                        0 1px 2px
                        rgba(0,0,0,0.05);
                }}

                .event-top {{
                    display: flex;
                    justify-content:
                        space-between;
                    align-items:
                        flex-start;
                    gap: 10px;
                }}

                .event-title {{
                    font-weight: 800;
                    font-size: 17px;
                }}

                .event-time {{
                    font-family:
                        ui-monospace,
                        SFMono-Regular,
                        Menlo,
                        monospace;
                    font-weight: 700;
                    white-space: nowrap;
                }}

                .event-tx {{
                    margin-top: 5px;
                    font-size: 13px;
                    color: #555;
                }}

                .event-detail {{
                    margin-top: 8px;
                    padding-top: 8px;
                    border-top:
                        1px solid
                        rgba(0,0,0,0.08);
                    font-family:
                        ui-monospace,
                        SFMono-Regular,
                        Menlo,
                        monospace;
                    font-size: 12px;
                    line-height: 1.45;
                    overflow-wrap: anywhere;
                }}

                .event-good {{
                    border-left-color: #198754;
                }}

                .event-lap {{
                    border-left-color: #0d6efd;
                    background: #eef5ff;
                }}

                .event-danger {{
                    border-left-color: #dc3545;
                    background: #fff0f1;
                }}

                .event-warning {{
                    border-left-color: #ffc107;
                }}

                .event-info {{
                    border-left-color: #0dcaf0;
                }}

                .event-muted {{
                    border-left-color: #adb5bd;
                }}

                .empty {{
                    background: #fff;
                    border-radius: 12px;
                    padding: 20px;
                    text-align: center;
                }}

                .back {{
                    display: block;
                    text-align: center;
                    text-decoration: none;
                    margin-top: 18px;
                    padding: 14px;
                    background: #333;
                    color: #fff;
                    border-radius: 10px;
                    font-weight: 700;
                }}

                @media (min-width: 600px) {{
                    .summary {{
                        grid-template-columns:
                            repeat(4, 1fr);
                    }}
                }}
            </style>
        </head>

        <body>
            <div class="page">

                <h1>ゲート診断</h1>

                <div class="note">
                    最新イベントを上に表示しています。
                    5秒ごとに自動更新します。
                    <br>
                    表示範囲：直近
                    <strong>{minutes}分</strong>
                </div>

                <div
                    class="current-status"
                    style="
                        background:{current_background};
                        border-color:{current_border};
                    "
                >
                    <div class="current-status-title">
                        {current_icon}
                        {html.escape(current_title)}
                    </div>

                    <div class="current-status-message">
                        {html.escape(current_message)}
                    </div>
                </div>

                <div class="summary">

                    <div class="summary-card">
                        <div class="summary-number">
                            {lap_count}
                        </div>
                        <div class="summary-label">
                            🏁 ラップ記録
                        </div>
                    </div>

                    <div class="summary-card">
                        <div class="summary-number">
                            {blocked_count}
                        </div>
                        <div class="summary-label">
                            ⛔ 最短時間除外
                        </div>
                    </div>

                    <div class="summary-card">
                        <div class="summary-number">
                            {entry_cancel_count}
                        </div>
                        <div class="summary-label">
                            ENTRY取消
                        </div>
                    </div>

                    <div class="summary-card">
                        <div class="summary-number">
                            {exit_cancel_count}
                        </div>
                        <div class="summary-label">
                            EXIT取消
                        </div>
                    </div>

                </div>

                <div class="range-buttons">
                    <a href="/admin/gate-diagnostics?minutes=5">
                        5分
                    </a>

                    <a href="/admin/gate-diagnostics?minutes=20">
                        20分
                    </a>

                    <a href="/admin/gate-diagnostics?minutes=60">
                        60分
                    </a>
                </div>

                {cards}

                <a
                    class="back"
                    href="/admin"
                >
                    管理者メニューへ戻る
                </a>

            </div>
        </body>
        </html>
        """
    )



@app.get("/admin", response_class=HTMLResponse)
def admin():
    setup_mode = get_setup_mode()

    product_mode = get_product_mode()

    product_mode_labels = {
        "lite": "Lite / 個人練習",
        "rider": "Rider / 中級競技",
        "event": "Event / 草レース運営",
        "facility": "Facility SaaS / 常設コース",
        "pro": "Pro Telemetry / プロ競技",
    }

    product_mode_label = product_mode_labels.get(product_mode, product_mode)
    is_lite = (product_mode == "lite")

    mode_labels = {
        "entry": "エントリー受付中",
        "locked": "エントリー締切",
        "race": "レース中",
        "finished": "レース終了",
    }

    mode_label = mode_labels.get(setup_mode, setup_mode)

    battery_rows = get_transmitter_battery_statuses()

    if battery_rows:
        battery_cards = ""
        current_time = time.time()

        for battery in battery_rows:
            serial_number = html.escape(
                str(battery["serial_number"])
            )

            receiver_rows_html = ""
            freshest_receiver = None

            for receiver in battery["receivers"]:
                updated_at = float(receiver["updated_at"])
                age_seconds = max(
                    0,
                    int(current_time - updated_at),
                )

                if age_seconds <= 120:
                    receive_status = "受信中"
                    status_class = "status-online"
                elif age_seconds <= 300:
                    receive_status = "直近"
                    status_class = "status-recent"
                else:
                    receive_status = "通信なし"
                    status_class = "status-offline"

                if (
                    freshest_receiver is None
                    or updated_at > freshest_receiver["updated_at"]
                ):
                    freshest_receiver = {
                        **receiver,
                        "updated_at": updated_at,
                    }

                receiver_id = html.escape(
                    str(receiver["receiver_id"])
                )

                receiver_rows_html += f"""
                <div class="receiver-row">
                    <div class="receiver-name">{receiver_id}</div>
                    <div class="{status_class}">
                        <strong>{receive_status}</strong>
                    </div>
                    <div>{age_seconds}秒前</div>
                    <div>{format_datetime(updated_at)}</div>
                </div>
                """

            if freshest_receiver is None:
                continue

            battery_percent = int(
                freshest_receiver["battery_percent"]
            )
            voltage_mv = int(
                freshest_receiver["voltage_mv"]
            )
            voltage_v = voltage_mv / 1000.0

            if battery_percent >= 70:
                battery_class = "battery-good"
            elif battery_percent >= 30:
                battery_class = "battery-warning"
            else:
                battery_class = "battery-low"

            battery_cards += f"""
            <div class="battery-card">
                <div class="battery-card-top">
                    <div class="battery-device">{serial_number}</div>
                    <div class="battery-percent {battery_class}">
                        {battery_percent}%
                    </div>
                </div>

                <div class="battery-voltage">
                    電圧: <strong>{voltage_v:.3f} V</strong>
                </div>

                <div class="receiver-list">
                    <div class="receiver-header">
                        <div>受信機</div>
                        <div>状態</div>
                        <div>経過</div>
                        <div>最終受信</div>
                    </div>
                    {receiver_rows_html}
                </div>
            </div>
            """

        battery_html = f"""
        <section class="battery-section">
            <h2>送信機バッテリー状態</h2>

            <div class="battery-card-list">
                {battery_cards}
            </div>

            <p class="battery-note">
                Battery情報は約60秒ごとに更新されます。
                各RXについて、最終受信から5分を超えると
                「通信なし」と表示します。
            </p>
        </section>
        """
    else:
        battery_html = """
        <section class="battery-section">
            <h2>送信機バッテリー状態</h2>
            <p>Battery情報はまだ受信していません。</p>
        </section>
        """

    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>管理者メニュー</title>
        <style>
            body {{
                font-family: sans-serif;
                padding: 16px;
            }}

            button {{
                font-size: 18px;
                padding: 12px 18px;
                margin-top: 10px;
                width: 100%;
            }}

            a {{
                display: block;
                margin-top: 14px;
                font-size: 18px;
            }}

            .battery-section {{
                margin: 24px 0;
                padding: 16px;
                border: 1px solid #ccc;
                border-radius: 8px;
            }}

            .battery-section h2 {{
                margin-top: 0;
            }}

            .battery-card-list {{
                display: grid;
                gap: 12px;
            }}

            .battery-card {{
                border: 1px solid #ddd;
                border-radius: 8px;
                padding: 14px;
                background: #fafafa;
            }}

            .battery-card-top {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 12px;
                margin-bottom: 12px;
            }}

            .battery-device {{
                font-size: 20px;
                font-weight: bold;
            }}

            .battery-percent {{
                font-size: 26px;
                font-weight: bold;
                padding: 6px 12px;
                border-radius: 999px;
            }}

            .battery-good {{
                background: #dff3e4;
                color: #176b2c;
            }}

            .battery-warning {{
                background: #fff1bf;
                color: #775a00;
            }}

            .battery-low {{
                background: #f8d7da;
                color: #8a1f2d;
            }}

            .battery-grid {{
                display: grid;
                grid-template-columns: 90px 1fr;
                gap: 8px 12px;
                align-items: center;
            }}

            .battery-label {{
                font-weight: bold;
                color: #555;
            }}

            .status-online {{
                color: #176b2c;
            }}

            .status-recent {{
                color: #775a00;
            }}

            .status-offline {{
                color: #8a1f2d;
            }}

            .battery-voltage {{
                margin-bottom: 14px;
                font-size: 17px;
            }}

            .receiver-list {{
                display: grid;
                gap: 6px;
            }}

            .receiver-header,
            .receiver-row {{
                display: grid;
                grid-template-columns: 90px 90px 80px 1fr;
                gap: 8px;
                align-items: center;
                padding: 7px 0;
            }}

            .receiver-header {{
                font-size: 13px;
                font-weight: bold;
                color: #555;
                border-bottom: 1px solid #ddd;
            }}

            .receiver-row {{
                border-bottom: 1px solid #eee;
            }}

            .receiver-name {{
                font-weight: bold;
            }}

            .battery-note {{
                margin-bottom: 0;
                font-size: 14px;
            }}

            @media (max-width: 520px) {{
                .battery-section {{
                    padding: 12px;
                }}

                .battery-grid {{
                    grid-template-columns: 78px 1fr;
                    font-size: 14px;
                }}

                .battery-device {{
                    font-size: 18px;
                }}

                .battery-percent {{
                    font-size: 22px;
                }}

                .receiver-header,
                .receiver-row {{
                    grid-template-columns: 72px 72px 62px 1fr;
                    gap: 6px;
                    font-size: 12px;
                }}

                .receiver-header {{
                    font-size: 11px;
                }}
            }}
        </style>
    </head>

    <body>
        <h1>管理者メニュー</h1>

        <p>
            製品モード:
            <strong>{product_mode_label}</strong>
        </p>

        {battery_html}

                {""
        if is_lite else
        '''
        <form action="/mode/entry" method="post">
            <button type="submit">エントリー開始</button>
        </form>

        <form action="/mode/locked" method="post">
            <button type="submit">エントリー終了</button>
        </form>

        <form action="/mode/race" method="post">
            <button type="submit">レース開始</button>
        </form>

        <form action="/mode/finished" method="post">
            <button type="submit">レース終了</button>
        </form>

        <a href="/entries">エントリー一覧</a>
        <a href="/devices">送信機管理</a>
        <a href="/majors">クラス管理</a>
        <a href="/races">過去レース一覧</a>
        '''
        }

        {"<a href='/lite/result' target='_blank'>リザルト</a>" if is_lite else "<a href='/live' target='_blank'>リザルト</a>"}

        <a href="/admin/rssi-monitor">RSSIモニタ</a>

        <a href="/admin/gate-diagnostics">🩺 ゲート診断</a>

        <a href="/admin/receivers">📡 レシーバー管理</a>

        <a href="/admin/rssi-settings">RSSI設定</a>

        <a href="/admin/rssi-analyze">RSSI自動分析</a>

        {""
        if not is_lite else
        '''
        <form action="/admin/lite-reset" method="post">
            <button type="submit">練習リセット</button>
        </form>
        '''
        }

        <a href="/admin/product-mode">製品モード設定</a>
        <a href="/admin/network">ネットワーク設定</a>
        <a href="/admin/shutdown">シャットダウン</a>
    </body>
    </html>
    """
@app.post("/admin/product-mode/save")
def product_mode_save(mode: str = Form(...)):
    set_product_mode(mode)

    return RedirectResponse(url="/admin/product-mode", status_code=303)

@app.get("/admin/network", response_class=HTMLResponse)
def network_page():

    try:
        current_ssid = subprocess.check_output(
            "iwgetid -r",
            shell=True
        ).decode().strip()
    except:
        current_ssid = "(未接続)"

    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>ネットワーク設定</title>

        <style>
            body {{
                font-family: sans-serif;
                padding: 16px;
            }}

            button {{
                width: 100%;
                font-size: 22px;
                padding: 18px;
                margin-top: 18px;
            }}
        </style>
    </head>

    <body>
        <a href="/admin">← 管理者メニューへ戻る</a>

        <h1>ネットワーク設定</h1>

        <p>
            現在のSSID:
            <strong>{current_ssid}</strong>
        </p>

        <form action="/admin/network/ap" method="post">
            <button type="submit">
                APモードへ切替
            </button>
        </form>

        <form action="/admin/network/home" method="post">
            <button type="submit">
                HOMEモードへ切替
            </button>
        </form>

        <p>
            APモード:
            SSID=LapTimer
            PASS=laptimer123
        </p>
    </body>
    </html>
    """

@app.post("/admin/network/ap")
def network_ap():

    subprocess.Popen(
        ["sudo", "/home/earth/lap-timer/network_ap.sh"]
    )

    return HTMLResponse("""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>

    <body>
        <h1>APモードへ切替中</h1>

        <p>
            数秒後に:
            <br>
            WiFi "LapTimer"
            <br>
            へ接続してください。
        </p>

        <p>
            URL:
            <br>
            http://192.168.4.1:8000
        </p>
    </body>
    </html>
    """)

@app.post("/admin/network/home")
def network_home():

    subprocess.Popen(
        ["sudo", "/home/earth/lap-timer/network_home.sh"]
    )

    return HTMLResponse("""
    <html>
    <body>
        <h1>HOMEモードへ切替中</h1>

        <p>
            数秒後に自宅WiFiへ戻ります。
        </p>
    </body>
    </html>
    """)    

@app.get("/admin/product-mode", response_class=HTMLResponse)
def product_mode_page():
    current_mode = get_product_mode()

    mode_labels = {
        "lite": "Lite / 個人練習",
        "rider": "Rider / 中級競技",
        "event": "Event / 草レース運営",
        "facility": "Facility SaaS / 常設コース",
        "pro": "Pro Telemetry / プロ競技",
    }

    options = ""

    for mode, label in mode_labels.items():
        selected = "selected" if mode == current_mode else ""

        options += f"""
        <option value="{mode}" {selected}>
            {label}
        </option>
        """

    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>製品モード設定</title>
        <style>
            body {{ font-family: sans-serif; padding: 16px; }}
            select {{ width: 100%; font-size: 18px; padding: 8px; }}
            button {{ margin-top: 18px; font-size: 18px; padding: 12px; width: 100%; }}
        </style>
    </head>
    <body>
        <a href="/admin">← 管理者メニューへ戻る</a>

        <h1>製品モード設定</h1>

        <p>現在のモード: <strong>{mode_labels.get(current_mode, current_mode)}</strong></p>

        <form action="/admin/product-mode/save" method="post">
            <select name="mode">
                {options}
            </select>

            <button type="submit">保存</button>
        </form>

        <h2>説明</h2>
        <p>Lite: 個人練習向け。将来的に電源ONで自動計測。</p>
        <p>Rider: 中級競技向け。簡易リザルトと個人管理。</p>
        <p>Event: 草レース運営向け。エントリー、クラス、CSV、履歴。</p>
        <p>Facility SaaS: 常設コース向け。クラウド連携予定。</p>
        <p>Pro Telemetry: プロ競技向け。GPS/IMU等予定。</p>
    </body>
    </html>
    """


@app.get("/admin/rssi-settings", response_class=HTMLResponse)
def rssi_settings():
    settings = get_rssi_settings()

    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>RSSI設定</title>
        <style>
            body {{ font-family: sans-serif; padding: 16px; }}
            label {{ display: block; margin-top: 14px; }}
            input {{ width: 100%; font-size: 18px; padding: 8px; }}
            button {{ margin-top: 18px; font-size: 18px; padding: 12px; width: 100%; }}
        </style>
    </head>
    <body>
        <a href="/admin">← 管理者メニューへ戻る</a>

        <h1>RSSI設定</h1>

        <form action="/admin/rssi-settings/save" method="post">
            <label>ENTER_RSSI_THRESHOLD（近づいた判定）</label>
            <input type="number" name="enter_rssi_threshold" value="{settings["enter_rssi_threshold"]}">

            <label>EXIT_RSSI_THRESHOLD（離れた判定）</label>
            <input type="number" name="exit_rssi_threshold" value="{settings["exit_rssi_threshold"]}">

            <label>COOLDOWN_SEC（通過後の無視秒数）</label>
            <input type="number" step="0.1" name="cooldown_sec" value="{settings["cooldown_sec"]}">

            <label>MIN_LAP_TIME_SEC（最低ラップ秒数）</label>
            <input type="number" step="0.1" name="min_lap_time_sec" value="{settings["min_lap_time_sec"]}">

            <button type="submit">保存</button>
        </form>

        <h2>目安</h2>
        <p>近接が -50 前後、離れが -70 前後なら、ENTER=-58 / EXIT=-68 から試してください。</p>
    </body>
    </html>
    """

@app.post("/admin/rssi-settings/save")
def rssi_settings_save(
    enter_rssi_threshold: int = Form(...),
    exit_rssi_threshold: int = Form(...),
    cooldown_sec: float = Form(...),
    min_lap_time_sec: float = Form(...),
):
    save_rssi_settings(
        enter_rssi_threshold,
        exit_rssi_threshold,
        cooldown_sec,
        min_lap_time_sec
    )

    return RedirectResponse(url="/admin/rssi-settings", status_code=303)

@app.get("/admin/rssi-analyze", response_class=HTMLResponse)
def rssi_analyze():
    result = analyze_rssi_logs()

    html_text = """
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>RSSI自動分析</title>
        <style>
            body { font-family: sans-serif; padding: 16px; }
            .card { background:#f5f5f5; padding:12px; margin:12px 0; }
            .big { font-size: 22px; font-weight: bold; }
            a { display:block; margin-top: 12px; }
        </style>
    </head>
    <body>
        <a href="/admin">← 管理者メニューへ戻る</a>
        <a href="/admin/rssi-monitor">RSSIモニタへ</a>

        <h1>RSSI自動分析</h1>
    """

    if result is None:
        html_text += """
        <div class="card">
            <p>ログが不足しています。</p>
            <p>送信機を近づけた状態と、離した状態でしばらくログを取ってください。</p>
            <p>最低20件以上必要です。</p>
        </div>
        """
    else:
        html_text += f"""
        <div class="card">
            <p>分析ログ数: {result["count"]}</p>
            <p>近接平均RSSI: <span class="big">{result["near_avg"]:.1f}</span></p>
            <p>離れ平均RSSI: <span class="big">{result["far_avg"]:.1f}</span></p>
            <p>RSSI差: <span class="big">{result["gap"]:.1f} dB</span></p>
            <p>安定度: <span class="big">{result["stability"]}</span></p>
        </div>

        <div class="card">
            <h2>推奨設定</h2>
            <p>ENTER_RSSI_THRESHOLD: <span class="big">{result["enter"]}</span></p>
            <p>EXIT_RSSI_THRESHOLD: <span class="big">{result["exit"]}</span></p>
        </div>

        <p>
            ENTERは「近づいた判定」、EXITは「離れた判定」です。
        </p>
        """

    html_text += """
    </body>
    </html>
    """

    return html_text

def analyze_rssi_logs():
    logs = get_latest_rssi_logs(300)

    if len(logs) < 20:
        return None

    rssi_values = sorted([log["rssi"] for log in logs])

    low_values = rssi_values[:20]
    high_values = rssi_values[-20:]

    near_avg = sum(high_values) / len(high_values)
    far_avg = sum(low_values) / len(low_values)

    gap = near_avg - far_avg

    enter = int((near_avg + far_avg) / 2 + 5)
    exit = int((near_avg + far_avg) / 2 - 5)

    if gap >= 25:
        stability = "かなり安定"
    elif gap >= 15:
        stability = "普通"
    else:
        stability = "不安定"

    return {
        "near_avg": near_avg,
        "far_avg": far_avg,
        "gap": gap,
        "enter": enter,
        "exit": exit,
        "stability": stability,
        "count": len(logs),
    }

@app.get("/admin/rssi-monitor", response_class=HTMLResponse)
def rssi_monitor():
    logs = get_latest_rssi_logs(200)
    settings = get_rssi_settings()

    now = time.time()
    health_window_sec = 30

    stats = {}
    recent_counts = {}

    for log in logs:
        mac = log["mac_address"]
        receiver_id = log.get("receiver_id") or "RX-UNKNOWN"

        key = (mac, receiver_id)

        log_created_at = log.get(
            "created_at",
            log.get("timestamp", 0),
        )

        if now - log_created_at <= health_window_sec:
            recent_counts[key] = recent_counts.get(key, 0) + 1

        if key not in stats:
            stats[key] = {
                "mac_address": mac,
                "receiver_id": receiver_id,
                "name": log["name"],
                "major": log["major"],
                "latest": log["rssi"],
                "min": log["rssi"],
                "max": log["rssi"],
                "sum": 0,
                "count": 0,
                "created_at": log.get(
                    "created_at",
                    log.get("timestamp", 0),
                ),
            }

        stats[key]["min"] = min(
            stats[key]["min"],
            log["rssi"],
        )
        stats[key]["max"] = max(
            stats[key]["max"],
            log["rssi"],
        )
        stats[key]["count"] += 1
        stats[key]["sum"] += log["rssi"]

        if log_created_at > stats[key]["created_at"]:
            stats[key]["created_at"] = log_created_at
            stats[key]["latest"] = log["rssi"]

    grouped = {}

    for _, s in stats.items():
        mac = s["mac_address"]

        if mac not in grouped:
            grouped[mac] = {
                "name": s["name"],
                "mac_address": mac,
                "receivers": [],
            }

        grouped[mac]["receivers"].append(s)

    html_text = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="5">
        <title>RSSIモニタ</title>

        <style>
            * {{
                box-sizing: border-box;
            }}

            body {{
                margin: 0;
                background: #f3f6fa;
                color: #172033;
                font-family:
                    -apple-system,
                    BlinkMacSystemFont,
                    "Segoe UI",
                    sans-serif;
            }}

            .page {{
                width: 100%;
                max-width: 760px;
                margin: 0 auto;
                padding: 18px 14px 32px;
            }}

            .topbar {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 12px;
                margin-bottom: 18px;
            }}

            .back-link {{
                color: #325bd6;
                text-decoration: none;
                font-size: 15px;
            }}

            .refresh {{
                font-size: 13px;
                color: #687386;
                white-space: nowrap;
            }}

            h1 {{
                margin: 4px 0 18px;
                font-size: 30px;
                line-height: 1.2;
            }}

            .settings-card {{
                background: white;
                border-radius: 16px;
                padding: 16px;
                margin-bottom: 16px;
                box-shadow: 0 3px 14px rgba(20, 35, 60, 0.08);
            }}

            .settings-title {{
                font-weight: 700;
                margin-bottom: 10px;
            }}

            .settings-grid {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 8px 12px;
                font-size: 14px;
            }}

            .settings-item {{
                background: #f7f9fc;
                border-radius: 10px;
                padding: 10px;
            }}

            .settings-label {{
                color: #667085;
                font-size: 12px;
                margin-bottom: 3px;
            }}

            .settings-value {{
                font-weight: 700;
                font-size: 18px;
            }}

            .links {{
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
                margin-bottom: 18px;
            }}

            .action-link {{
                display: inline-block;
                background: white;
                color: #325bd6;
                text-decoration: none;
                border-radius: 10px;
                padding: 10px 12px;
                font-size: 14px;
                box-shadow: 0 2px 8px rgba(20, 35, 60, 0.06);
            }}

            .device-card {{
                background: white;
                border-radius: 18px;
                padding: 16px;
                margin-bottom: 18px;
                box-shadow: 0 4px 16px rgba(20, 35, 60, 0.09);
            }}

            .device-header {{
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                gap: 12px;
                margin-bottom: 14px;
            }}

            .device-name {{
                font-size: 21px;
                font-weight: 800;
            }}

            .tx-name {{
                color: #667085;
                font-size: 14px;
                font-weight: 600;
                margin-top: 2px;
            }}

            .rx-list {{
                display: grid;
                gap: 12px;
            }}

            .rx-card {{
                border-radius: 14px;
                padding: 14px;
                border: 1px solid #e4e9f2;
                background: #fbfcfe;
            }}

            .rx-top {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 10px;
            }}

            .rx-id {{
                font-size: 17px;
                font-weight: 800;
                white-space: nowrap;
            }}

            .status-badge {{
                display: inline-flex;
                align-items: center;
                gap: 6px;
                padding: 6px 10px;
                border-radius: 999px;
                font-size: 13px;
                font-weight: 700;
                white-space: nowrap;
            }}

            .status-online {{
                color: #126a35;
                background: #dcf4e6;
            }}

            .status-recent {{
                color: #8a6500;
                background: #fff2c2;
            }}

            .status-old {{
                color: #9d2936;
                background: #fde2e5;
            }}

            .dot {{
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: currentColor;
            }}

            .rssi-row {{
                display: flex;
                justify-content: space-between;
                align-items: flex-end;
                gap: 10px;
                margin-top: 12px;
            }}

            .rssi-value {{
                font-size: 42px;
                line-height: 1;
                font-weight: 850;
                letter-spacing: -1px;
            }}

            .dbm {{
                font-size: 16px;
                color: #667085;
                font-weight: 600;
                margin-left: 4px;
            }}

            .judge {{
                font-size: 14px;
                font-weight: 700;
                color: #42526a;
                background: #eef2f7;
                border-radius: 10px;
                padding: 7px 10px;
                white-space: nowrap;
            }}

            .meta-grid {{
                display: grid;
                grid-template-columns: repeat(4, 1fr);
                gap: 8px;
                margin-top: 14px;
            }}

            .meta-item {{
                background: white;
                border: 1px solid #edf0f4;
                border-radius: 10px;
                padding: 9px 6px;
                text-align: center;
            }}

            .meta-label {{
                color: #7a8494;
                font-size: 11px;
                margin-bottom: 3px;
            }}

            .meta-value {{
                font-size: 14px;
                font-weight: 700;
                white-space: nowrap;
            }}

            .health-ok {{
                margin: 0 0 14px;
                padding: 11px 13px;
                border-radius: 12px;
                background: #e4f6ea;
                color: #176b36;
                font-size: 14px;
                font-weight: 700;
            }}

            .health-warning {{
                margin: 0 0 14px;
                padding: 12px 13px;
                border-radius: 12px;
                background: #fff2c2;
                color: #765900;
                font-size: 14px;
                font-weight: 700;
            }}

            .health-danger {{
                margin: 0 0 14px;
                padding: 12px 13px;
                border-radius: 12px;
                background: #fde2e5;
                color: #982938;
                font-size: 14px;
                font-weight: 800;
            }}

            .health-detail {{
                margin-top: 4px;
                font-size: 12px;
                font-weight: 600;
                opacity: 0.85;
            }}

            .packet-count {{
                margin-top: 8px;
                color: #667085;
                font-size: 12px;
                font-weight: 600;
            }}

            .balance-card {{
                margin-top: 14px;
                background: #fff7d8;
                border: 1px solid #f2df8f;
                border-radius: 12px;
                padding: 12px;
                text-align: center;
            }}

            .balance-title {{
                font-size: 12px;
                color: #806a17;
                margin-bottom: 3px;
            }}

            .balance-value {{
                font-size: 24px;
                font-weight: 850;
                color: #6c5812;
            }}

            .help-card {{
                background: white;
                border-radius: 16px;
                padding: 16px;
                box-shadow: 0 3px 14px rgba(20, 35, 60, 0.07);
            }}

            .help-card h2 {{
                font-size: 18px;
                margin: 0 0 10px;
            }}

            .help-card p {{
                font-size: 14px;
                line-height: 1.6;
                margin: 6px 0;
                color: #4e5968;
            }}

            .empty {{
                background: white;
                border-radius: 16px;
                padding: 24px;
                text-align: center;
                color: #667085;
            }}

            @media (max-width: 520px) {{
                .page {{
                    padding: 14px 12px 26px;
                }}

                h1 {{
                    font-size: 28px;
                }}

                .settings-grid {{
                    grid-template-columns: 1fr 1fr;
                }}

                .meta-grid {{
                    grid-template-columns: repeat(2, 1fr);
                }}

                .rssi-value {{
                    font-size: 38px;
                }}
            }}
        </style>
    </head>

    <body>
        <div class="page">

            <div class="topbar">
                <a class="back-link" href="/admin">
                    ← 管理者メニュー
                </a>

                <div class="refresh">
                    5秒ごとに自動更新
                </div>
            </div>

            <h1>RSSIモニタ</h1>

            <div class="settings-card">
                <div class="settings-title">
                    現在のRSSI設定
                </div>

                <div class="settings-grid">
                    <div class="settings-item">
                        <div class="settings-label">
                            ENTER
                        </div>
                        <div class="settings-value">
                            {settings["enter_rssi_threshold"]}
                        </div>
                    </div>

                    <div class="settings-item">
                        <div class="settings-label">
                            EXIT
                        </div>
                        <div class="settings-value">
                            {settings["exit_rssi_threshold"]}
                        </div>
                    </div>

                    <div class="settings-item">
                        <div class="settings-label">
                            COOLDOWN
                        </div>
                        <div class="settings-value">
                            {settings["cooldown_sec"]} 秒
                        </div>
                    </div>

                    <div class="settings-item">
                        <div class="settings-label">
                            MIN LAP
                        </div>
                        <div class="settings-value">
                            {settings["min_lap_time_sec"]} 秒
                        </div>
                    </div>
                </div>
            </div>

            <div class="links">
                <a class="action-link" href="/admin/rssi-settings">
                    RSSI設定を変更
                </a>

                <a class="action-link" href="/admin/rssi-analyze">
                    RSSI自動分析
                </a>

                <a class="action-link" href="/admin/rssi-pass-log">
                    通過検証ログ
                </a>
            </div>
    """

    if not grouped:
        html_text += """
            <div class="empty">
                RSSIログなし
            </div>
        """
    else:
        for mac, device in grouped.items():
            transmitter_name = transmitter_name_from_mac(mac)

            receivers = sorted(
                device["receivers"],
                key=lambda item: item["receiver_id"],
            )

            rx_values = []

            receiver_health = {}

            for receiver in receivers:
                receiver_id = receiver["receiver_id"]
                key = (mac, receiver_id)

                count_30s = recent_counts.get(key, 0)
                age_sec = int(now - receiver["created_at"])

                receiver_health[receiver_id] = {
                    "count": count_30s,
                    "age": age_sec,
                }

            health_class = "health-ok"
            health_title = "✓ 受信状態 正常"
            health_detail = "RX-0001 / RX-0002 とも受信しています"

            rx1_health = receiver_health.get("RX-0001")
            rx2_health = receiver_health.get("RX-0002")

            if rx1_health and rx2_health:
                c1 = rx1_health["count"]
                c2 = rx2_health["count"]
                a1 = rx1_health["age"]
                a2 = rx2_health["age"]

                max_count = max(c1, c2)
                min_count = min(c1, c2)

                weak_rx = "RX-0001" if c1 < c2 else "RX-0002"

                ratio = (
                    min_count / max_count
                    if max_count > 0
                    else 1.0
                )

                if (
                    (a1 > 10 and a2 <= 3)
                    or (a2 > 10 and a1 <= 3)
                ):
                    health_class = "health-danger"
                    health_title = f"⚠ {weak_rx} 受信停止の可能性"
                    health_detail = "レシーバーの再起動を確認してください"

                elif max_count >= 8 and ratio < 0.10:
                    health_class = "health-danger"
                    health_title = f"⚠ {weak_rx} 受信異常の可能性"
                    health_detail = (
                        f"直近30秒: RX-0001 {c1}件 / "
                        f"RX-0002 {c2}件　"
                        "再起動を確認してください"
                    )

                elif max_count >= 8 and ratio < 0.30:
                    health_class = "health-warning"
                    health_title = f"△ {weak_rx} 受信頻度が低下"
                    health_detail = (
                        f"直近30秒: RX-0001 {c1}件 / "
                        f"RX-0002 {c2}件"
                    )

                else:
                    health_detail = (
                        f"直近30秒: RX-0001 {c1}件 / "
                        f"RX-0002 {c2}件"
                    )

            html_text += f"""
            <section class="device-card">

                <div class="device-header">
                    <div>
                        <div class="device-name">
                            {html.escape(device["name"] or "")}
                        </div>

                        <div class="tx-name">
                            {html.escape(transmitter_name)}
                        </div>
                    </div>
                </div>

                <div class="{health_class}">
                    {health_title}
                    <div class="health-detail">
                        {health_detail}
                    </div>
                </div>

                <div class="rx-list">
            """

            for s in receivers:
                latest = s["latest"]
                avg = s["sum"] / s["count"]
                receiver_id = s["receiver_id"]

                age_sec = int(
                    time.time() - s["created_at"]
                )

                if age_sec <= 3:
                    status = "受信中"
                    status_class = "status-online"
                elif age_sec <= 10:
                    status = "直近"
                    status_class = "status-recent"
                else:
                    status = "古い"
                    status_class = "status-old"

                if latest >= -55:
                    judge = "近接"
                elif latest <= -70:
                    judge = "離れ"
                else:
                    judge = "中間"

                rx_values.append(
                    (
                        receiver_id,
                        latest,
                    )
                )

                html_text += f"""
                    <div class="rx-card">

                        <div class="rx-top">
                            <div class="rx-id">
                                {html.escape(receiver_id)}
                            </div>

                            <div class="status-badge {status_class}">
                                <span class="dot"></span>
                                {status}
                            </div>
                        </div>

                        <div class="rssi-row">
                            <div>
                                <span class="rssi-value">
                                    {latest}
                                </span>
                                <span class="dbm">
                                    dBm
                                </span>
                            </div>

                            <div class="judge">
                                {judge}
                            </div>
                        </div>

                        <div class="packet-count">
                            直近30秒の受信ログ:
                            {recent_counts.get((mac, receiver_id), 0)}件
                        </div>

                        <div class="meta-grid">

                            <div class="meta-item">
                                <div class="meta-label">
                                    最終受信
                                </div>
                                <div class="meta-value">
                                    {age_sec}秒前
                                </div>
                            </div>

                            <div class="meta-item">
                                <div class="meta-label">
                                    最大
                                </div>
                                <div class="meta-value">
                                    {s["max"]}
                                </div>
                            </div>

                            <div class="meta-item">
                                <div class="meta-label">
                                    最小
                                </div>
                                <div class="meta-value">
                                    {s["min"]}
                                </div>
                            </div>

                            <div class="meta-item">
                                <div class="meta-label">
                                    平均
                                </div>
                                <div class="meta-value">
                                    {avg:.1f}
                                </div>
                            </div>

                        </div>

                    </div>
                """

            html_text += """
                </div>
            """

            if len(rx_values) >= 2:
                rx_map = {
                    receiver_id: rssi
                    for receiver_id, rssi in rx_values
                }

                if (
                    "RX-0001" in rx_map
                    and "RX-0002" in rx_map
                ):
                    difference = abs(
                        rx_map["RX-0001"]
                        - rx_map["RX-0002"]
                    )

                    html_text += f"""
                    <div class="balance-card">
                        <div class="balance-title">
                            RX-0001 / RX-0002 RSSI差
                        </div>

                        <div class="balance-value">
                            {difference} dB
                        </div>
                    </div>
                    """

            html_text += """
            </section>
            """

    html_text += """
            <div class="help-card">
                <h2>使い方</h2>

                <p>
                    通過位置でRX-0001とRX-0002の
                    RSSIを確認します。
                </p>

                <p>
                    2台の値が近いほど、
                    左右バランスの確認がしやすくなります。
                </p>

                <p>
                    送信機を離した位置でも確認し、
                    ENTER / EXIT の設定調整に使用してください。
                </p>
            </div>

        </div>
    </body>
    </html>
    """

    return html_text



# ============================================================
# リセット後クリア待ち状態
# ============================================================

def get_latest_gate_state_event():
    db_path = os.path.join(
        os.path.dirname(__file__),
        "lap_timer.db",
    )

    conn = sqlite3.connect(
        db_path,
        timeout=5.0,
    )

    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                serial_number,
                event_type,
                timestamp,
                detail
            FROM gate_state_events
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """
        )

        row = cur.fetchone()

        if row is None:
            return None

        return dict(row)

    except sqlite3.OperationalError:
        return None

    finally:
        conn.close()


def get_gate_status_html():
    event = get_latest_gate_state_event()

    if event is None:
        return (
            '<div style="'
            'margin:0 0 16px 0;'
            'padding:12px 14px;'
            'border-radius:10px;'
            'background:#f3f4f6;'
            'font-weight:700;'
            '">'
            '⚪ 計測状態：履歴なし'
            '</div>'
        )

    event_type = event.get(
        "event_type"
    )

    event_time = event.get(
        "timestamp"
    )

    time_text = ""

    if event_time is not None:
        time_text = time.strftime(
            "%H:%M:%S",
            time.localtime(event_time),
        )

    if event_type == "RESET_WAITING_CLEAR":
        return (
            '<div style="'
            'margin:0 0 16px 0;'
            'padding:14px;'
            'border-radius:10px;'
            'background:#fff3cd;'
            'border:1px solid #ffca2c;'
            '">'
            '<div style="font-weight:800;">'
            '🟠 リセット後クリア待ち'
            '</div>'
            '<div style="margin-top:5px;">'
            'TXをゲートから十分離してください。'
            '<br>'
            '周回計測はまだ開始されていません。'
            '</div>'
            f'<div style="margin-top:5px;font-size:0.85em;">'
            f'状態更新：{time_text}'
            '</div>'
            '</div>'
        )

    if event_type == "MEASUREMENT_READY":
        return (
            '<div style="'
            'margin:0 0 16px 0;'
            'padding:14px;'
            'border-radius:10px;'
            'background:#d1e7dd;'
            'border:1px solid #75b798;'
            '">'
            '<div style="font-weight:800;">'
            '🟢 計測準備OK'
            '</div>'
            '<div style="margin-top:5px;">'
            '次の通過から周回を記録します。'
            '</div>'
            f'<div style="margin-top:5px;font-size:0.85em;">'
            f'状態更新：{time_text}'
            '</div>'
            '</div>'
        )

    return (
        '<div style="'
        'margin:0 0 16px 0;'
        'padding:12px 14px;'
        'border-radius:10px;'
        'background:#f3f4f6;'
        '">'
        '⚪ 計測状態：確認中'
        '</div>'
    )


# ============================================================
# RSSI 通過検証ログ
# ============================================================

def get_rssi_pass_events(
    minutes: int = 20,
    max_events: int = 20,
):
    """
    RSSIログから通過イベントを抽出する。

    判定:
    1. ENTER以上で通過開始
    2. 通過中のRSSI推移を保持
    3. ENTER到達後、RSSIがEXIT以下まで低下したら通過完了
    4. EXITまで確認できない場合はEXIT未完了として残す

    表示用に通過前後4秒のログも含める。
    """

    settings = get_rssi_settings()

    enter_threshold = settings[
        "enter_rssi_threshold"
    ]

    exit_threshold = settings[
        "exit_rssi_threshold"
    ]

    db_path = os.path.join(
        os.path.dirname(__file__),
        "lap_timer.db",
    )

    since = time.time() - (minutes * 60)
    current_time = time.time()

    conn = sqlite3.connect(
        db_path,
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            mac_address,
            receiver_id,
            name,
            major,
            rssi,
            timestamp
        FROM rssi_logs
        WHERE receiver_id IN (
            'RX-0001',
            'RX-0002'
        )
          AND timestamp >= ?
        ORDER BY
            mac_address ASC,
            timestamp ASC
        """,
        (since,),
    )

    log_rows = [
        dict(row)
        for row in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT
            name,
            lap_number,
            timestamp
        FROM laps
        WHERE timestamp >= ?
        ORDER BY timestamp ASC
        """,
        (since,),
    )

    lap_rows = [
        dict(row)
        for row in cur.fetchall()
    ]

    gate_state_rows = []

    try:
        cur.execute(
            """
            SELECT
                serial_number,
                event_type,
                timestamp,
                detail
            FROM gate_state_events
            WHERE timestamp >= ?
            ORDER BY timestamp ASC, id ASC
            """,
            (
                since - 60.0,
            ),
        )

        gate_state_rows = [
            dict(row)
            for row in cur.fetchall()
        ]

    except sqlite3.OperationalError:
        gate_state_rows = []

    conn.close()

    logs_by_mac = {}

    for row in log_rows:
        mac = row["mac_address"]

        logs_by_mac.setdefault(
            mac,
            [],
        ).append(row)

    events = []

    # 通過前後の確認範囲
    context_sec = 4.0

    # ENTER後、最低これだけ経過してから
    # EXIT判定を有効にする
    min_exit_delay_sec = 1.0

    # EXIT未完了のイベントを無限に引き延ばさないための上限
    max_event_sec = 30.0

    for mac, rows in logs_by_mac.items():

        if not rows:
            continue

        inside = False
        event_start = None
        event_end = None
        exit_completed = False

        raw_events = []

        last_rssi_by_receiver = {
            "RX-0001": None,
            "RX-0002": None,
        }

        last_time_by_receiver = {
            "RX-0001": None,
            "RX-0002": None,
        }

        exit_candidate_since = None

        for row in rows:
            timestamp = row["timestamp"]
            rssi = row["rssi"]
            receiver_id = row["receiver_id"]

            last_rssi_by_receiver[
                receiver_id
            ] = rssi

            last_time_by_receiver[
                receiver_id
            ] = timestamp

            rx1_rssi = last_rssi_by_receiver[
                "RX-0001"
            ]
            rx2_rssi = last_rssi_by_receiver[
                "RX-0002"
            ]

            rx1_time = last_time_by_receiver[
                "RX-0001"
            ]
            rx2_time = last_time_by_receiver[
                "RX-0002"
            ]

            # 本番プログラムと同じ考え方で、
            # 両RXのデータが新鮮か確認する
            both_fresh = (
                rx1_time is not None
                and rx2_time is not None
                and timestamp - rx1_time <= 0.60
                and timestamp - rx2_time <= 0.60
            )

            fresh_values = []

            if (
                rx1_time is not None
                and timestamp - rx1_time <= 0.60
                and rx1_rssi is not None
            ):
                fresh_values.append(rx1_rssi)

            if (
                rx2_time is not None
                and timestamp - rx2_time <= 0.60
                and rx2_rssi is not None
            ):
                fresh_values.append(rx2_rssi)

            combined_rssi = (
                max(fresh_values)
                if fresh_values
                else None
            )

            entry_signal = (
                combined_rssi is not None
                and combined_rssi >= enter_threshold
            )

            # ------------------------------------------------
            # 本番受信プログラムと同じEXIT判定
            #
            # RXごとに
            # ・RSSIがEXIT閾値未満
            #       または
            # ・最後の受信から0.60秒超
            #
            # のどちらかならEXIT側とみなす。
            #
            # 一度も受信していないRXはEXIT扱いしない。
            # ------------------------------------------------

            rx1_has_data = (
                rx1_time is not None
                and rx1_rssi is not None
            )

            rx2_has_data = (
                rx2_time is not None
                and rx2_rssi is not None
            )

            rx1_timed_out = (
                rx1_has_data
                and timestamp - rx1_time > 0.60
            )

            rx2_timed_out = (
                rx2_has_data
                and timestamp - rx2_time > 0.60
            )

            rx1_below_exit = (
                rx1_has_data
                and rx1_rssi < exit_threshold
            )

            rx2_below_exit = (
                rx2_has_data
                and rx2_rssi < exit_threshold
            )

            rx1_exit_ready = (
                rx1_has_data
                and (
                    rx1_below_exit
                    or rx1_timed_out
                )
            )

            rx2_exit_ready = (
                rx2_has_data
                and (
                    rx2_below_exit
                    or rx2_timed_out
                )
            )

            both_below_exit = (
                rx1_exit_ready
                and rx2_exit_ready
            )

            # --------------------------------------------
            # 待機中 → ENTER
            # --------------------------------------------
            if not inside:

                if entry_signal:
                    inside = True
                    event_start = timestamp
                    event_end = timestamp
                    exit_completed = False
                    exit_candidate_since = None

                continue

            # --------------------------------------------
            # 通過中
            # --------------------------------------------
            event_end = timestamp

            elapsed = (
                timestamp - event_start
            )

            # --------------------------------------------
            # EXIT候補
            #
            # 本番と同様に
            # 両RXが「弱RSSIまたは受信タイムアウト」
            # の状態を1.0秒継続したらEXIT成立
            # --------------------------------------------
            if (
                elapsed >= min_exit_delay_sec
                and both_below_exit
            ):
                if exit_candidate_since is None:
                    exit_candidate_since = timestamp

                exit_elapsed = (
                    timestamp
                    - exit_candidate_since
                )

                if exit_elapsed >= 1.0:
                    raw_events.append(
                        {
                            "start": event_start,
                            "end": timestamp,
                            "exit_completed": True,
                            "exit_timestamp": timestamp,
                            "exit_receiver_id":
                                "RX-0001+RX-0002",
                            "exit_rssi": None,
                            "exit_rx1_rssi":
                                rx1_rssi,
                            "exit_rx2_rssi":
                                rx2_rssi,
                        }
                    )

                    inside = False
                    event_start = None
                    event_end = None
                    exit_completed = True
                    exit_candidate_since = None
                    continue

            else:
                # 両RXがEXIT側でなくなったら
                # EXIT候補をキャンセル
                exit_candidate_since = None

            # 30秒以上EXITできなければ、
            # 「EXIT未完了」として一度確定
            if elapsed >= max_event_sec:
                raw_events.append(
                    {
                        "start": event_start,
                        "end": timestamp,
                        "exit_completed": False,
                        "exit_timestamp": None,
                        "exit_receiver_id": None,
                        "exit_rssi": None,
                        "exit_rx1_rssi": None,
                        "exit_rx2_rssi": None,
                    }
                )

                inside = False
                event_start = None
                event_end = None
                exit_completed = False
                exit_candidate_since = None

        # ログの最後までENTER状態が続いていた場合
        if inside and event_start is not None:

            raw_events.append(
                {
                    "start": event_start,
                    "end": (
                        event_end
                        if event_end is not None
                        else current_time
                    ),
                    "exit_completed": False,
                    "exit_timestamp": None,
                    "exit_receiver_id": None,
                    "exit_rssi": None,
                }
            )

        # ====================================================
        # 各イベントを詳細解析
        # ====================================================

        for raw_event in raw_events:

            active_start = raw_event["start"]
            active_end = raw_event["end"]

            window_start = (
                active_start - context_sec
            )

            window_end = (
                active_end + context_sec
            )

            samples = [
                row
                for row in rows
                if (
                    window_start
                    <= row["timestamp"]
                    <= window_end
                )
            ]

            if not samples:
                continue

            rx1_samples = [
                row
                for row in samples
                if row["receiver_id"]
                == "RX-0001"
            ]

            rx2_samples = [
                row
                for row in samples
                if row["receiver_id"]
                == "RX-0002"
            ]

            rx1_peak = (
                max(
                    row["rssi"]
                    for row in rx1_samples
                )
                if rx1_samples
                else None
            )

            rx2_peak = (
                max(
                    row["rssi"]
                    for row in rx2_samples
                )
                if rx2_samples
                else None
            )

            peak_difference = None

            if (
                rx1_peak is not None
                and rx2_peak is not None
            ):
                peak_difference = abs(
                    rx1_peak - rx2_peak
                )

            strongest = max(
                samples,
                key=lambda row: row["rssi"],
            )

            peak_time = strongest["timestamp"]

            name = (
                strongest["name"]
                or ""
            )

            matched_laps = [
                lap
                for lap in lap_rows
                if (
                    lap["name"] == name
                    and window_start
                    <= lap["timestamp"]
                    <= window_end
                )
            ]

            # --------------------------------------------
            # 周回記録なしの理由判定
            #
            # 通過ピーク時点で最後の状態イベントが
            # RESET_WAITING_CLEAR なら、
            # リセット後クリア待ち中だったと判定する。
            #
            # 現在はTX-0003 1台運用のため、
            # 最新の状態履歴を時刻で照合する。
            # --------------------------------------------

            no_lap_reason = None

            if not matched_laps:
                latest_gate_event = None

                for gate_event in gate_state_rows:
                    if (
                        gate_event["timestamp"]
                        <= peak_time
                    ):
                        latest_gate_event = gate_event
                    else:
                        break

                if (
                    latest_gate_event is not None
                    and latest_gate_event[
                        "event_type"
                    ] == "RESET_WAITING_CLEAR"
                ):
                    no_lap_reason = (
                        "リセット後クリア待ち"
                    )

            # --------------------------------------------
            # 1秒単位でRX1/RX2の最大RSSIをまとめる
            # --------------------------------------------

            bins = {}

            for sample in samples:

                second = int(
                    sample["timestamp"]
                )

                if second not in bins:
                    bins[second] = {
                        "timestamp": second,
                        "RX-0001": None,
                        "RX-0002": None,
                    }

                receiver_id = sample[
                    "receiver_id"
                ]

                old_rssi = bins[second].get(
                    receiver_id
                )

                if (
                    old_rssi is None
                    or sample["rssi"] > old_rssi
                ):
                    bins[second][
                        receiver_id
                    ] = sample["rssi"]

            timeline = []

            for second in sorted(bins):

                item = bins[second]

                r1 = item["RX-0001"]
                r2 = item["RX-0002"]

                difference = None

                if (
                    r1 is not None
                    and r2 is not None
                ):
                    difference = abs(
                        r1 - r2
                    )

                phase = ""

                if second < int(active_start):
                    phase = "接近前"

                elif second > int(active_end):
                    phase = "通過後"

                elif raw_event["exit_completed"]:
                    phase = "通過中"

                else:
                    phase = "EXIT待ち"

                timeline.append(
                    {
                        "timestamp":
                            item["timestamp"],
                        "rx1": r1,
                        "rx2": r2,
                        "difference":
                            difference,
                        "phase":
                            phase,
                    }
                )

            duration = max(
                0.0,
                active_end - active_start,
            )

            events.append(
                {
                    "mac_address": mac,
                    "name": name,

                    "peak_time":
                        peak_time,

                    "active_start":
                        active_start,

                    "active_end":
                        active_end,

                    "duration":
                        duration,

                    "exit_completed":
                        raw_event[
                            "exit_completed"
                        ],

                    "exit_timestamp":
                        raw_event.get(
                            "exit_timestamp"
                        ),

                    "exit_receiver_id":
                        raw_event.get(
                            "exit_receiver_id"
                        ),

                    "exit_rssi":
                        raw_event.get(
                            "exit_rssi"
                        ),

                    "exit_rx1_rssi":
                        raw_event.get(
                            "exit_rx1_rssi"
                        ),

                    "exit_rx2_rssi":
                        raw_event.get(
                            "exit_rx2_rssi"
                        ),

                    "window_start":
                        window_start,

                    "window_end":
                        window_end,

                    "rx1_peak":
                        rx1_peak,

                    "rx2_peak":
                        rx2_peak,

                    "peak_difference":
                        peak_difference,

                    "rx1_count":
                        len(rx1_samples),

                    "rx2_count":
                        len(rx2_samples),

                    "matched_laps":
                        matched_laps,

                    "no_lap_reason":
                        no_lap_reason,

                    "timeline":
                        timeline,

                    "raw_samples":
                        samples,
                }
            )

    events.sort(
        key=lambda event:
            event["peak_time"],
        reverse=True,
    )

    return events[:max_events]


@app.get(
    "/admin/rssi-pass-log",
    response_class=HTMLResponse,
)
def rssi_pass_log():

    gate_status_html = (
        get_gate_status_html()
    )

    events = get_rssi_pass_events(
        minutes=20,
        max_events=20,
    )

    html_text = """
    <html>
    <head>
        <meta charset="utf-8">
        <meta
            name="viewport"
            content="width=device-width, initial-scale=1"
        >

        <title>RSSI 通過検証ログ</title>

        <style>
            * {
                box-sizing: border-box;
            }

            body {
                margin: 0;
                background: #f3f6fa;
                color: #172033;
                font-family:
                    -apple-system,
                    BlinkMacSystemFont,
                    "Segoe UI",
                    sans-serif;
            }

            .page {
                width: 100%;
                max-width: 760px;
                margin: 0 auto;
                padding: 16px 12px 32px;
            }

            a {
                color: #325bd6;
                text-decoration: none;
            }

            h1 {
                margin: 18px 0 10px;
                font-size: 28px;
            }

            .note {
                background: white;
                border-radius: 14px;
                padding: 14px;
                margin: 14px 0 18px;
                color: #566174;
                font-size: 13px;
                line-height: 1.6;
                box-shadow:
                    0 3px 12px
                    rgba(20, 35, 60, 0.07);
            }

            .event-card {
                background: white;
                border-radius: 16px;
                padding: 15px;
                margin-bottom: 16px;
                box-shadow:
                    0 4px 16px
                    rgba(20, 35, 60, 0.09);
            }

            .event-top {
                display: flex;
                justify-content:
                    space-between;
                align-items: flex-start;
                gap: 10px;
            }

            .event-time {
                font-size: 20px;
                font-weight: 800;
            }

            .event-name {
                margin-top: 4px;
                color: #667085;
                font-size: 14px;
            }

            .lap-ok {
                background: #dcf4e6;
                color: #126a35;
                padding: 7px 10px;
                border-radius: 999px;
                font-size: 12px;
                font-weight: 700;
                white-space: nowrap;
            }

            .exit-ok {
                background: #dcf4e6;
                color: #126a35;
                padding: 7px 10px;
                border-radius: 999px;
                font-size: 12px;
                font-weight: 700;
                white-space: nowrap;
            }

            .exit-ng {
                background: #fde2e5;
                color: #982938;
                padding: 7px 10px;
                border-radius: 999px;
                font-size: 12px;
                font-weight: 800;
                white-space: nowrap;
            }

            .badge-row {
                display: flex;
                flex-direction: column;
                align-items: flex-end;
                gap: 6px;
            }

            .lap-none {
                background: #fff2c2;
                color: #765900;
                padding: 7px 10px;
                border-radius: 999px;
                font-size: 12px;
                font-weight: 700;
                white-space: nowrap;
            }

            .summary {
                display: grid;
                grid-template-columns:
                    repeat(2, 1fr);
                gap: 8px;
                margin-top: 14px;
            }

            .summary-item {
                background: #f7f9fc;
                border-radius: 10px;
                padding: 10px;
                text-align: center;
            }

            .label {
                color: #7a8494;
                font-size: 11px;
            }

            .value {
                margin-top: 3px;
                font-size: 18px;
                font-weight: 800;
            }

            details {
                margin-top: 14px;
            }

            summary {
                cursor: pointer;
                color: #325bd6;
                font-weight: 700;
                padding: 8px 0;
            }

            .table-wrap {
                overflow-x: auto;
            }

            table {
                width: 100%;
                border-collapse: collapse;
                margin-top: 8px;
                font-size: 13px;
            }

            th,
            td {
                padding: 8px 6px;
                border-bottom:
                    1px solid #e5e9ef;
                text-align: center;
                white-space: nowrap;
            }

            th {
                color: #687386;
                font-size: 11px;
            }

            .empty {
                background: white;
                border-radius: 14px;
                padding: 24px;
                text-align: center;
                color: #667085;
            }
        </style>
    </head>

    <body>
        <div class="page">

            <a href="/admin/rssi-monitor">
                ← RSSIモニタへ戻る
            </a>

            <h1>通過検証ログ</h1>

            <div class="note">
                直近20分のRSSIログから、
                ENTER以上になった区間を
                「通過候補」として抽出しています。
                候補区間の前後4秒も表示するため、
                RSSIが強くなって再び弱くなる様子を
                確認できます。
            </div>
    """

    html_text = html_text.replace(
        "<h1>通過検証ログ</h1>",
        gate_status_html
        + "<h1>通過検証ログ</h1>",
        1,
    )

    if not events:
        html_text += """
            <div class="empty">
                通過候補はありません。
            </div>
        """

    for index, event in enumerate(
        events,
        start=1,
    ):
        transmitter_name = (
            transmitter_name_from_mac(
                event["mac_address"]
            )
        )

        time_text = time.strftime(
            "%H:%M:%S",
            time.localtime(
                event["peak_time"]
            ),
        )

        if event["matched_laps"]:
            lap_badge = (
                '<span class="lap-ok">'
                '✓ 周回記録あり'
                '</span>'
            )
        else:
            lap_badge = (
                '<span class="lap-none">'
                '△ 周回記録なし'
                '</span>'
            )

            no_lap_reason = event.get(
                "no_lap_reason"
            )

            if no_lap_reason:
                lap_badge += (
                    '<div style="'
                    'margin-top:6px;'
                    'font-weight:700;'
                    '">'
                    f'理由：{no_lap_reason}'
                    '</div>'
                )

        if event["exit_completed"]:
            exit_badge = (
                '<span class="exit-ok">'
                '✓ EXIT完了'
                '</span>'
            )
        else:
            exit_badge = (
                '<span class="exit-ng">'
                '⚠ EXIT未完了'
                '</span>'
            )

        duration_text = (
            f'{event["duration"]:.1f} 秒'
        )

        if event["exit_completed"] and event["exit_timestamp"] is not None:
            exit_time_text = time.strftime(
                "%H:%M:%S",
                time.localtime(
                    event["exit_timestamp"]
                ),
            )

            exit_rx1_text = (
                "-"
                if event["exit_rx1_rssi"] is None
                else f'{event["exit_rx1_rssi"]} dBm'
            )

            exit_rx2_text = (
                "-"
                if event["exit_rx2_rssi"] is None
                else f'{event["exit_rx2_rssi"]} dBm'
            )

            exit_detail_text = (
                f'{exit_time_text}<br>'
                f'成立RX: {event["exit_receiver_id"]}<br>'
                f'RX-0001: {exit_rx1_text}<br>'
                f'RX-0002: {exit_rx2_text}'
            )
        else:
            exit_detail_text = "未成立"

        rx1_peak = (
            "-"
            if event["rx1_peak"] is None
            else str(event["rx1_peak"])
        )

        rx2_peak = (
            "-"
            if event["rx2_peak"] is None
            else str(event["rx2_peak"])
        )

        peak_diff = (
            "-"
            if event["peak_difference"]
            is None
            else (
                f'{event["peak_difference"]} dB'
            )
        )

        html_text += f"""
        <section class="event-card">

            <div class="event-top">
                <div>
                    <div class="event-time">
                        通過候補 #{index}
                       　{time_text}
                    </div>

                    <div class="event-name">
                        {html.escape(event["name"])}
                        /
                        {html.escape(transmitter_name)}
                    </div>
                </div>

                <div class="badge-row">
                    {lap_badge}
                    {exit_badge}
                </div>
            </div>

            <div class="summary">

                <div class="summary-item">
                    <div class="label">
                        RX-0001 最大
                    </div>
                    <div class="value">
                        {rx1_peak} dBm
                    </div>
                </div>

                <div class="summary-item">
                    <div class="label">
                        RX-0002 最大
                    </div>
                    <div class="value">
                        {rx2_peak} dBm
                    </div>
                </div>

                <div class="summary-item">
                    <div class="label">
                        最大値差
                    </div>
                    <div class="value">
                        {peak_diff}
                    </div>
                </div>

                <div class="summary-item">
                    <div class="label">
                        受信ログ数
                    </div>
                    <div class="value">
                        {event["rx1_count"]}
                        /
                        {event["rx2_count"]}
                    </div>
                </div>

                <div class="summary-item">
                    <div class="label">
                        ENTER → EXIT
                    </div>
                    <div class="value">
                        {duration_text}
                    </div>
                </div>

                <div class="summary-item">
                    <div class="label">
                        EXIT成立
                    </div>
                    <div class="value" style="font-size:14px;">
                        {exit_detail_text}
                    </div>
                </div>

            </div>

            <details>
                <summary>
                    RSSI推移を見る
                </summary>

                <div class="table-wrap">
                    <table>
                        <tr>
                            <th>時刻</th>
                            <th>RX-0001</th>
                            <th>RX-0002</th>
                            <th>差</th>
                            <th>状態</th>
                        </tr>
        """

        for item in event["timeline"]:

            row_time = time.strftime(
                "%H:%M:%S",
                time.localtime(
                    item["timestamp"]
                ),
            )

            rx1 = (
                "-"
                if item["rx1"] is None
                else str(item["rx1"])
            )

            rx2 = (
                "-"
                if item["rx2"] is None
                else str(item["rx2"])
            )

            diff = (
                "-"
                if item["difference"]
                is None
                else (
                    f'{item["difference"]}'
                )
            )

            html_text += f"""
                        <tr>
                            <td>{row_time}</td>
                            <td>{rx1}</td>
                            <td>{rx2}</td>
                            <td>{diff}</td>
                            <td>{item["phase"]}</td>
                        </tr>
            """

        html_text += """
                    </table>
                </div>
            </details>

            <details>
                <summary>
                    高密度RSSI生ログを見る
                </summary>

                <div class="table-wrap">
                    <table>
                        <tr>
                            <th>時刻</th>
                            <th>受信機</th>
                            <th>RSSI</th>
                            <th>状態</th>
                        </tr>
        """

        for sample in event["raw_samples"]:

            sample_time = time.strftime(
                "%H:%M:%S",
                time.localtime(
                    sample["timestamp"]
                ),
            )

            milliseconds = int(
                (
                    sample["timestamp"]
                    - int(sample["timestamp"])
                )
                * 1000
            )

            sample_time_text = (
                f"{sample_time}.{milliseconds:03d}"
            )

            if sample["timestamp"] < event["active_start"]:
                sample_phase = "接近前"

            elif sample["timestamp"] > event["active_end"]:
                sample_phase = "通過後"

            elif event["exit_completed"]:
                sample_phase = "通過中"

            else:
                sample_phase = "EXIT待ち"

            html_text += f"""
                        <tr>
                            <td>{sample_time_text}</td>
                            <td>{html.escape(sample["receiver_id"])}</td>
                            <td>{sample["rssi"]} dBm</td>
                            <td>{sample_phase}</td>
                        </tr>
            """

        html_text += """
                    </table>
                </div>
            </details>

        </section>
        """

    html_text += """
        </div>
    </body>
    </html>
    """

    return html_text


@app.get("/races", response_class=HTMLResponse)
def races():
    rows = get_races()

    html_text = """
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>過去レース一覧</title>
        <style>
            body { font-family: sans-serif; padding: 16px; }
            table { width: 100%; border-collapse: collapse; margin-top: 16px; }
            th, td { border-bottom: 1px solid #ccc; padding: 8px; text-align: left; }
            th { background: #eee; }
        </style>
    </head>
    <body>
        <a href="/admin">← 管理者メニューへ戻る</a>

        <h1>過去レース一覧</h1>

        <table>
            <tr>
                <th>ID</th>
                <th>レース名</th>
                <th>状態</th>
                <th>開始</th>
                <th>終了</th>
                <th>リザルト</th>
            </tr>
    """

    for row in rows:
        started_at = format_datetime(row["started_at"])
        finished_at = format_datetime(row["finished_at"])

        html_text += f"""
        <tr>
            <td>{row["id"]}</td>
            <td>{html.escape(row["name"] or "")}</td>
            <td>{html.escape(row["status"])}</td>
            <td>{started_at}</td>
            <td>{finished_at}</td>
            <td>
                <a href="/races/{row["id"]}">
                    表示
                </a>
            </td>
        </tr>
        """

    html_text += """
        </table>
    </body>
    </html>
    """

    return html_text

@app.get("/races/{race_id}", response_class=HTMLResponse)
def race_result(race_id: int):
    rows = get_overall_ranking_by_race(race_id)
    major_rankings = get_major_rankings_by_race(race_id)

    html_text = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>過去レースリザルト</title>
        <style>
            body {{ font-family: sans-serif; padding: 16px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
            th, td {{ border-bottom: 1px solid #ccc; padding: 8px; text-align: left; }}
            th {{ background: #eee; }}
        </style>
    </head>
    <body>
        <a href="/races">← 過去レース一覧へ戻る</a>

        <h1>過去レースリザルト #{race_id}</h1>
    <p>
    <a href="/races/{race_id}/csv">CSVダウンロード</a>
    </p>
    """

    html_text += ranking_table(
        "総合順位",
        rows
    )

    for group in major_rankings:
        html_text += ranking_table(
            group["label"],
            group["ranking"],
            group["major"]
        )

    html_text += """
    </body>
    </html>
    """

    return html_text

@app.get("/races/{race_id}/csv")
def race_result_csv(race_id: int):
    rows = get_overall_ranking_by_race(race_id)

    csv_text = "順位,ゼッケン,Name,チーム名,周回数,Best,Avg\n"

    for i, row in enumerate(rows, start=1):
        minor = row["minor"] if row["minor"] is not None else ""
        name = row["name"] or ""
        team_name = row["team_name"] or ""
        laps = row["laps"] or 0
        best = format_time(row["best_lap"])
        avg = format_time(row["avg_lap"])

        csv_text += f"{i},{minor},{name},{team_name},{laps},{best},{avg}\n"

    return Response(
        content=csv_text.encode("utf-8-sig"),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="race_{race_id}_result.csv"'
        }
    )

@app.get("/entries", response_class=HTMLResponse)
def entries():
    groups = get_entries_by_major()

    html_text = """
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>エントリーリスト</title>
        <style>
            body { font-family: sans-serif; padding: 16px; }
            table { width: 100%; border-collapse: collapse; margin-top: 8px; margin-bottom: 28px; }
            th, td { border-bottom: 1px solid #ccc; padding: 8px; text-align: left; }
            th { background: #eee; }
        </style>
    </head>
    <body>
        <a href="/">← 参加者ページへ戻る</a>
        <h1>エントリーリスト</h1>
    """

    for group in groups:
        html_text += f"""
        <h2>{html.escape(group["label"])}（{len(group["entries"])}台）</h2>
            <table>
            <tr>
                <th>ゼッケン</th>
                <th>Name</th>
                <th>車種</th>
                <th>チーム名</th>
                <th>編集</th>
            </tr>
        """

        if not group["entries"]:
            html_text += """
            <tr>
                <td colspan="5">エントリーなし</td>
            </tr>
            """

        for entry in group["entries"]:
            html_text += f"""
            <tr>
                <td>{entry["minor"]}</td>
                <td>{html.escape(entry["name"])}</td>
                <td>{html.escape(entry["model_name"] or "")}</td>
                <td>{html.escape(entry["team_name"] or "")}</td>
                <td>
                    <a href="/admin/entry-edit/{entry["mac_address"]}">
                        編集
                    </a>
                </td>
            </tr>
            """

        html_text += """
        </table> 
        """   

    html_text += """
    </body>
    </html>
    """

    return html_text


@app.get("/entry", response_class=HTMLResponse)
def entry():
    setup_mode = get_setup_mode()

    if setup_mode != "entry":
        return f"""
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>エントリー不可</title>
        </head>
        <body>
            <a href="/view">← 順位一覧へ戻る</a>
            <h1>現在エントリーできません</h1>
            <p>現在の状態: {html.escape(setup_mode)}</p>
            <p>主催者がエントリー開始を押すまで登録できません。</p>
        </body>
        </html>
        """
    return """
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>レースエントリー</title>
        <style>
            body { font-family: sans-serif; padding: 16px; }
            button { font-size: 20px; padding: 12px 20px; }
        </style>
    </head>
    <body>
        <a href="/">← 参加者ページへ戻る</a>

        <h1>レースエントリー</h1>

        <p>送信機を受信機の近くに置いてください。</p>

        <form action="/entry/scan" method="post">
            <button type="submit">レースエントリー開始</button>
        </form>
    </body>
    </html>
    """


@app.post("/entry/scan")
def entry_scan():
    setup_mode = get_setup_mode()

    if setup_mode != "entry":
        return HTMLResponse("""
        <html>
        <body>
            <h1>エントリー受付中ではありません</h1>
            <p>主催者がエントリー開始を押してください。</p>
            <a href="/">参加者ページへ戻る</a>
        </body>
        </html>
        """)

    mac_address = scan_and_register_sync()

    if mac_address is None:
        return HTMLResponse("""
        <html>
        <body>
            <h1>送信機が見つかりません</h1>
            <p>送信機を近づけて、登録モード中に再実行してください。</p>
            <a href="/entry">戻る</a>
        </body>
        </html>
        """)

    return RedirectResponse(url=f"/entry/{mac_address}", status_code=303)


@app.get("/entry/{mac_address}", response_class=HTMLResponse)
def entry_edit(mac_address: str):
    device = get_device_by_mac(mac_address)
    majors = get_majors()

    if device is None:
        return "<h1>Device not found</h1>"

    current_major = device.get("major", 1)

    major_options = ""

    for m in majors:
        selected = "selected" if m["major"] == current_major else ""

        major_options += f"""
        <option value="{m["major"]}" {selected}>
            {html.escape(m["label"])}
        </option>
        """

    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>エントリー確定</title>
        <style>
            body {{ font-family: sans-serif; padding: 16px; }}
            label {{ display: block; margin-top: 12px; }}
            input, select {{ width: 100%; font-size: 18px; padding: 8px; }}
            button {{ margin-top: 16px; font-size: 18px; padding: 10px 16px; }}
        </style>
    </head>
    <body>
        <a href="/entry">← エントリーへ戻る</a>

        <h1>エントリー確定</h1>

        <p>ゼッケン: {device["minor"]}</p>
        <p>MAC: {mac_address}</p>

        <form action="/entry/{mac_address}/save" method="post">
            <label>参加クラス</label>
            <select name="major">
                {major_options}
            </select>

            <label>Name</label>
            <input name="name" value="{html.escape(device["name"])}">

            <label>車種</label>
            <input name="model_name" value="{html.escape(device["model_name"] or "")}">

            <label>チーム名</label>
            <input name="team_name" value="{html.escape(device["team_name"] or "")}">

            <button type="submit">エントリー確定</button>
        </form>
    </body>
    </html>
    """


@app.post("/entry/{mac_address}/save")
def entry_save(
    mac_address: str,
    major: int = Form(...),
    name: str = Form(...),
    model_name: str = Form(""),
    team_name: str = Form(""),
):
    setup_mode = get_setup_mode()

    if setup_mode != "entry":
        return HTMLResponse("""
        <html>
        <body>
            <h1>エントリー受付中ではありません</h1>
            <p>主催者がエントリー開始を押してください。</p>
            <a href="/">参加者ページへ戻る</a>
        </body>
        </html>
        """)


    upsert_device(
        mac_address,
        name,
        model_name,
        team_name,
        major
    )

    return RedirectResponse(url="/entries", status_code=303)


@app.get("/devices", response_class=HTMLResponse)
def devices():
    devices = get_all_devices()

    html_text = """
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Devices</title>
        <style>
            body { font-family: sans-serif; padding: 16px; }
            table { width: 100%; border-collapse: collapse; }
            th, td { border-bottom: 1px solid #ccc; padding: 8px; text-align: left; }
            th { background: #eee; }
        </style>
    </head>
    <body>
        <a href="/admin">← 管理者メニューへ戻る</a>
        <h1>送信機管理</h1>

        <table>
            <tr>
                <th>Name</th>
                <th>車種</th>
                <th>チーム</th>
                <th>MAC</th>
                <th>編集</th>
            </tr>
    """

    for d in devices:
        html_text += f"""
        <tr>
            <td>{html.escape(d["name"])}</td>
            <td>{html.escape(d["model_name"] or "")}</td>
            <td>{html.escape(d["team_name"] or "")}</td>
            <td>{d["mac_address"]}</td>
            <td><a href="/edit-device/{d["mac_address"]}">編集</a></td>
        </tr>
        """

    html_text += """
        </table>
    </body>
    </html>
    """

    return html_text



@app.get("/edit-device/{mac_address}", response_class=HTMLResponse)
def edit_device(mac_address: str):
    device = get_device_by_mac(mac_address)
    majors = get_majors()

    if device is None:
        return "<h1>Device not found</h1>"

    current_major = device.get("major", 1)

    major_options = ""

    for m in majors:
        selected = "selected" if m["major"] == current_major else ""

        major_options += f"""
        <option value="{m["major"]}" {selected}>
            {html.escape(m["label"])}
        </option>
        """

    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Edit Device</title>
        <style>
            body {{ font-family: sans-serif; padding: 16px; }}
            label {{ display: block; margin-top: 12px; }}
            input, select {{ width: 100%; font-size: 18px; padding: 8px; }}
            button {{ margin-top: 16px; font-size: 18px; padding: 10px 16px; }}
        </style>
    </head>
    <body>
        <a href="/admin">← 管理者メニューへ戻る</a>

        <h1>デバイス編集</h1>

        <p>MAC: {mac_address}</p>
        <p>ゼッケン: {device["minor"]}</p>

        <form action="/edit-device/{mac_address}/save" method="get">
            <label>クラス</label>
            <select name="major">
                {major_options}
            </select>

            <label>Name</label>
            <input name="name" value="{html.escape(device["name"])}">

            <label>車種</label>
            <input name="model_name" value="{html.escape(device["model_name"] or "")}">

            <label>チーム名</label>
            <input name="team_name" value="{html.escape(device["team_name"] or "")}">

            <button type="submit">ESP32へ書き込み</button>
        </form>
    </body>
    </html>
    """


@app.get("/edit-device/{mac_address}/save")
def save_device(
    mac_address: str,
    name: str,
    model_name: str = "",
    team_name: str = "",
    major: int = 1,
):
    asyncio.run(
        write_device_info(
            mac_address,
            name,
            model_name,
            team_name
        )
    )

    upsert_device(
        mac_address,
        name,
        model_name,
        team_name,
        major
    )

    return RedirectResponse(url="/devices", status_code=303)


@app.get("/majors", response_class=HTMLResponse)
def majors():
    major_list = get_majors()

    html_text = """
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>クラス管理</title>
        <style>
            body { font-family: sans-serif; padding: 16px; }
            table { width: 100%; border-collapse: collapse; margin-top: 16px; }
            th, td { border-bottom: 1px solid #ccc; padding: 8px; text-align: left; }
            th { background: #eee; }
            input, button { font-size: 16px; padding: 8px; margin-top: 6px; }
        </style>
    </head>
    <body>
        <a href="/admin">← 管理者メニューへ戻る</a>

        <h1>クラス管理</h1>

        <table>
            <tr>
                <th>Major</th>
                <th>表示名</th>
                <th>編集</th>
            </tr>
    """

    for m in major_list:
        html_text += f"""
        <tr>
            <td>{m["major"]}</td>
            <td>{html.escape(m["label"])}</td>
            <td>
                <form action="/majors/save" method="post">
                    <input type="hidden" name="major" value="{m["major"]}">
                    <input name="label" value="{html.escape(m["label"])}">
                    <button type="submit">更新</button>
                </form>
            </td>
        </tr>
        """

    html_text += """
        </table>

        <h2>新規クラス追加</h2>

        <form action="/majors/save" method="post">
            <label>Major番号</label><br>
            <input name="major" type="number" required><br>

            <label>表示名</label><br>
            <input name="label" required><br>

            <button type="submit">追加</button>
        </form>
    </body>
    </html>
    """

    return html_text


@app.post("/majors/save")
def save_major(major: int = Form(...), label: str = Form(...)):
    add_or_update_major(major, label)
    return RedirectResponse(url="/majors", status_code=303)


@app.get("/driver/{name}", response_class=HTMLResponse)
def driver_detail(name: str, major: int | None = None):
    conn = sqlite3.connect("lap_timer.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if major is None:
        cur.execute("""
        SELECT lap_number, lap_time, timestamp, major
        FROM laps
        WHERE name = ?
        ORDER BY lap_number ASC
        """, (name,))
    else:
        cur.execute("""
        SELECT lap_number, lap_time, timestamp, major
        FROM laps
        WHERE name = ?
          AND major = ?
        ORDER BY lap_number ASC
        """, (name, major))

    laps = cur.fetchall()

    if major is None:
        cur.execute("""
        SELECT
            COUNT(*) AS lap_count,
            MIN(NULLIF(lap_time, 0)) AS best_lap,
            AVG(NULLIF(lap_time, 0)) AS avg_lap
        FROM laps
        WHERE name = ?
        """, (name,))
    else:
        cur.execute("""
        SELECT
            COUNT(*) AS lap_count,
            MIN(NULLIF(lap_time, 0)) AS best_lap,
            AVG(NULLIF(lap_time, 0)) AS avg_lap
        FROM laps
        WHERE name = ?
          AND major = ?
        """, (name, major))

    summary = cur.fetchone()

    if major is None:
        cur.execute("""
        SELECT lap_number, name, timestamp, major
        FROM laps
        ORDER BY lap_number ASC, timestamp ASC
        """)
    else:
        cur.execute("""
        SELECT lap_number, name, timestamp, major
        FROM laps
        WHERE major = ?
        ORDER BY lap_number ASC, timestamp ASC
        """, (major,))

    all_laps = cur.fetchall()

    major_label = None

    if major is not None:
        cur.execute("""
        SELECT label
        FROM majors
        WHERE major = ?
        """, (major,))

        row = cur.fetchone()

        if row is not None:
            major_label = row["label"]

    conn.close()

    lap_groups = {}

    for row in all_laps:
        lap_no = row["lap_number"]

        if lap_no not in lap_groups:
            lap_groups[lap_no] = []

        lap_groups[lap_no].append(row)

    position_history = []

    for lap_no in sorted(lap_groups.keys()):
        sorted_rows = sorted(
            lap_groups[lap_no],
            key=lambda r: r["timestamp"]
        )

        for position, row in enumerate(sorted_rows, start=1):
            if row["name"] == name:
                position_history.append({
                    "lap": lap_no,
                    "position": position
                })

    labels = [p["lap"] for p in position_history]
    position_map = {
    p["lap"]: p["position"]
    for p in position_history
}
    best_lap_value = summary["best_lap"]
    positions = [p["position"] for p in position_history]

    if major is None:
        graph_title = "総合順位推移"
        page_title = name
        lap_list_title = "全ラップ（総合）"
    else:
        graph_title = f"{major_label or 'クラス'} 順位推移"
        page_title = f"{name} / {major_label or 'クラス'}"
        lap_list_title = f"全ラップ（{major_label or 'クラス'}）"

    html_text = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{html.escape(page_title)}</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{ font-family: sans-serif; padding: 16px; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ border-bottom: 1px solid #ccc; padding: 8px; text-align: left; }}
            th {{ background: #eee; }}
            .card {{ background: #f5f5f5; padding: 12px; margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <a href="/">← 参加者ページへ戻る</a>

        <h1>{html.escape(page_title)}</h1>

        <div class="card">
            <div>周回数: {summary["lap_count"]}</div>
            <div>ベストラップ: {format_time(summary["best_lap"])}</div>
            <div>平均ラップ: {format_time(summary["avg_lap"])}</div>
        </div>

        <h2>{html.escape(graph_title)}</h2>
        <canvas id="positionChart"></canvas>

        <h2>{html.escape(lap_list_title)}</h2>
        <table>
            <tr>
                <th>Lap</th>
                <th>順位</th>
                <th>Lap Time</th>
            </tr>
    """
  
    for lap in laps:
        lap_text = "START" if lap["lap_time"] == 0 else format_time(lap["lap_time"])

        row_style = ""

        if (
            best_lap_value is not None
            and lap["lap_time"] != 0
            and abs(lap["lap_time"] - best_lap_value) < 0.001
        ):
            row_style = ' style="font-weight:bold;"'

        position = position_map.get(
            lap["lap_number"],
            "-"
        )

        html_text += f"""
        <tr{row_style}>
            <td>{lap["lap_number"]}</td>
            <td>{position}</td>
            <td>{lap_text}</td>
        </tr>
        """
    html_text += f"""
        </table>

        <script>
            const ctx = document.getElementById('positionChart');

            new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: {labels},
                    datasets: [{{
                        label: '順位',
                        data: {positions},
                        tension: 0.2
                    }}]
                }},
                options: {{
                    scales: {{
                        y: {{
                            reverse: true,
                            ticks: {{
                                stepSize: 1
                            }}
                        }}
                    }}
                }}
            }});
        </script>
    </body>
    </html>
    """

    return html_text
