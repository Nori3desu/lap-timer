from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import sqlite3
import asyncio
import html
import time

from ranking import (
    get_ranking,
    get_overall_ranking,
    get_major_rankings,
)

from database import (
    init_db,
    clear_laps,
    set_race_active,
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
    
)

from ble_device_writer import write_device_info
from ble_entry import scan_and_register_sync
from backup import backup_db

app = FastAPI()


@app.on_event("startup")
def startup():
    init_db()


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

@app.get("/", response_class=HTMLResponse)
def root():
    setup_mode = get_setup_mode()

    mode_labels = {
        "entry": "エントリー受付中",
        "locked": "エントリー締切",
        "race": "レース中",
        "finished": "レース終了",
    }

    mode_label = mode_labels.get(setup_mode, setup_mode)

    return f"""

    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">

        <title>Lap Timer</title>

        <style>
            body {{
                font-family: sans-serif;
                padding: 20px;
                text-align: center;
            }}

            h1 {{
                margin-bottom: 30px;
            }}

            button {{
                width: 100%;
                max-width: 320px;
                font-size: 24px;
                padding: 18px;
                margin-top: 20px;
            }}
        </style>
    </head>

    <body>
        <h1>Lap Timer</h1>
        <p>
            現在の状態: <strong>{mode_label}</strong>
        </p>
      
        <p>
            <a href="/entry">
                <button>レースエントリー</button>
            </a>
        </p>

                <p>
            <a href="/entries">
                <button>エントリーリスト</button>
            </a>
        </p>

                <p>
            <a href="/live">
                <button>リザルト</button>
            </a>
        </p>
        
    </body>
    </html>
    """


@app.get("/ranking")
def ranking():
    return get_ranking()


@app.post("/race/reset")
def reset_race():
    clear_laps()
    set_setup_mode("race")
    set_race_active(True)

    return RedirectResponse(url="/view", status_code=303)

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

@app.get("/admin", response_class=HTMLResponse)
def admin():
    setup_mode = get_setup_mode()

    mode_labels = {
        "entry": "エントリー受付中",
        "locked": "エントリー締切",
        "race": "レース中",
        "finished": "レース終了",
    }

    mode_label = mode_labels.get(setup_mode, setup_mode)

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
        </style>
    </head>

    <body>
        <h1>管理者メニュー</h1>

        <p>
            現在の状態:
            <strong>{mode_label}</strong>
        </p>

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
        <a href="/live" target="_blank">リザルト</a>
        <a href="/races">過去レース一覧</a>
    </body>
    </html>
    """

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
                <th>車名</th>
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

            <label>車名</label>
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
                <th>車名</th>
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

            <label>車名</label>
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