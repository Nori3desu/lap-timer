import asyncio
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bleak import BleakScanner, BleakClient
from database import save_lap as save_web_lap


# ============================================================
# Raspberry Pi 5 BLEラップタイマー
#
# 機能:
#   1. 登録済みiBeaconを受信
#   2. RSSIによる通過判定
#   3. 同じ通過を重複記録しない
#   4. ラップ時刻・ラップタイムをSQLiteへ保存
#   5. 省電力送信機をBLE接続でコース内モードへ移行
# ============================================================


# ------------------------------------------------------------
# 保存先
# ------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "lap_timer.sqlite3"
WEB_DATABASE_PATH = BASE_DIR / "lap_timer.db"
RESET_REQUEST_PATH = BASE_DIR / "lite_reset.request"
RSSI_LOG_INTERVAL_SECONDS = 1.0


# ------------------------------------------------------------
# ラップ判定設定
# ------------------------------------------------------------

ENTRY_RSSI_THRESHOLD = -55
EXIT_RSSI_THRESHOLD = -65
MINIMUM_LAP_SECONDS = 10.0

SCAN_RESTART_SECONDS = 60
DISPLAY_INTERVAL_SECONDS = 0.25

ENTRY_CONFIRM_SECONDS = 0.8
EXIT_CONFIRM_SECONDS = 1.5


# ------------------------------------------------------------
# 省電力送信機 BLE接続設定
# ------------------------------------------------------------

# Pi 5が送信機へBLE接続を維持する時間
KEEP_ALIVE_CONNECT_SECONDS = 15.0

# 接続失敗時などに、同じ送信機へ再接続を試す最短間隔
CONNECT_RETRY_INTERVAL_SECONDS = 60.0

# 送信機側のコース内モード継続時間
# 送信機側が4時間連続送信する仕様に合わせる
COURSE_MODE_SKIP_SECONDS = 4 * 60 * 60

# BLE接続を試す最低RSSI
# -127 dBm のような異常値や弱すぎる電波では接続しない
CONNECT_MIN_RSSI = -90

# すでにRSSIを受信できている送信機には、無理に何度もBLE接続しない
# 接続失敗しても、この時間は再接続を控える
SEEN_SKIP_SECONDS = 10 * 60


# ------------------------------------------------------------
# 登録送信機
# ------------------------------------------------------------

@dataclass(frozen=True)
class RegisteredTransmitter:
    serial_number: str
    uuid: str
    major: int
    minor: int
    rider_name: str
    bike_name: str


REGISTERED_TRANSMITTERS = [
    RegisteredTransmitter(
        serial_number="TX-0001",
        uuid="12345678-1234-1234-1234-123456789abc",
        major=1,
        minor=1,
        rider_name="河村",
        bike_name="YZ250FX",
    ),
]


# ------------------------------------------------------------
# 送信機ごとの動作状態
# ------------------------------------------------------------

@dataclass
class TransmitterState:
    inside_detection_zone: bool = False
    last_lap_monotonic: float | None = None
    last_lap_datetime: datetime | None = None
    lap_count: int = 0
    last_display_monotonic: float = 0.0
    last_rssi: int | None = None
    last_rssi_log_monotonic: float = 0.0
    waiting_for_clear_after_reset: bool = False
    entry_candidate_since: float | None = None
    exit_candidate_since: float | None = None


transmitter_states: dict[str, TransmitterState] = {
    transmitter.serial_number: TransmitterState()
    for transmitter in REGISTERED_TRANSMITTERS
}


# ------------------------------------------------------------
# 実行状態
# ------------------------------------------------------------

running = True

# BLE接続中の送信機ID
connected_serial_numbers: set[str] = set()

# BLE接続処理中の送信機ID
connecting_serial_numbers: set[str] = set()

# 最後にBLE接続を試した時刻
# key: serial_number
last_connect_attempt_monotonic: dict[str, float] = {}

# コース内モード扱いの終了時刻
# key: serial_number
course_mode_until_monotonic: dict[str, float] = {}

# スキャン停止後に接続するための予約
# key: serial_number
pending_connect_requests: dict[str, tuple[RegisteredTransmitter, object]] = {}


# ------------------------------------------------------------
# SQLite処理
# ------------------------------------------------------------

def open_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS transmitters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial_number TEXT NOT NULL UNIQUE,
            uuid TEXT NOT NULL,
            major INTEGER NOT NULL,
            minor INTEGER NOT NULL,
            rider_name TEXT NOT NULL,
            bike_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS laps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial_number TEXT NOT NULL,
            lap_number INTEGER NOT NULL,
            passed_at TEXT NOT NULL,
            lap_time_seconds REAL,
            rssi INTEGER NOT NULL,
            uuid TEXT NOT NULL,
            major INTEGER NOT NULL,
            minor INTEGER NOT NULL,
            device_address TEXT,
            device_name TEXT,
            FOREIGN KEY(serial_number)
                REFERENCES transmitters(serial_number)
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_laps_serial_number
        ON laps(serial_number, lap_number)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_laps_passed_at
        ON laps(passed_at)
        """
    )

    connection.commit()
    return connection


def register_transmitters(connection: sqlite3.Connection) -> None:
    now = datetime.now().isoformat(timespec="seconds")

    for transmitter in REGISTERED_TRANSMITTERS:
        connection.execute(
            """
            INSERT INTO transmitters (
                serial_number,
                uuid,
                major,
                minor,
                rider_name,
                bike_name,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(serial_number) DO UPDATE SET
                uuid = excluded.uuid,
                major = excluded.major,
                minor = excluded.minor,
                rider_name = excluded.rider_name,
                bike_name = excluded.bike_name
            """,
            (
                transmitter.serial_number,
                transmitter.uuid.lower(),
                transmitter.major,
                transmitter.minor,
                transmitter.rider_name,
                transmitter.bike_name,
                now,
            ),
        )

    connection.commit()


def load_existing_lap_counts(connection: sqlite3.Connection) -> None:
    for transmitter in REGISTERED_TRANSMITTERS:
        row = connection.execute(
            """
            SELECT
                lap_number,
                passed_at
            FROM laps
            WHERE serial_number = ?
            ORDER BY lap_number DESC
            LIMIT 1
            """,
            (transmitter.serial_number,),
        ).fetchone()

        if row is None:
            continue

        lap_number, passed_at = row
        state = transmitter_states[transmitter.serial_number]

        state.lap_count = int(lap_number)

        try:
            state.last_lap_datetime = datetime.fromisoformat(passed_at)
        except ValueError:
            state.last_lap_datetime = None


def save_lap(
    connection: sqlite3.Connection,
    transmitter: RegisteredTransmitter,
    lap_number: int,
    passed_at: datetime,
    lap_time_seconds: float | None,
    rssi: int,
    uuid: str,
    major: int,
    minor: int,
    device_address: str,
    device_name: str,
) -> None:
    connection.execute(
        """
        INSERT INTO laps (
            serial_number,
            lap_number,
            passed_at,
            lap_time_seconds,
            rssi,
            uuid,
            major,
            minor,
            device_address,
            device_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transmitter.serial_number,
            lap_number,
            passed_at.isoformat(timespec="milliseconds"),
            lap_time_seconds,
            rssi,
            uuid,
            major,
            minor,
            device_address,
            device_name,
        ),
    )

    connection.commit()


# ------------------------------------------------------------
# iBeacon解析
# ------------------------------------------------------------

def format_uuid(uuid_bytes: bytes) -> str:
    value = uuid_bytes.hex()

    return (
        f"{value[0:8]}-"
        f"{value[8:12]}-"
        f"{value[12:16]}-"
        f"{value[16:20]}-"
        f"{value[20:32]}"
    )


def parse_ibeacon(company_id: int, data: bytes):
    if company_id != 0x004C:
        return None

    if len(data) < 23:
        return None

    if data[0:2] != b"\x02\x15":
        return None

    return {
        "uuid": format_uuid(data[2:18]).lower(),
        "major": int.from_bytes(data[18:20], byteorder="big"),
        "minor": int.from_bytes(data[20:22], byteorder="big"),
        "tx_power": int.from_bytes(
            data[22:23],
            byteorder="big",
            signed=True,
        ),
    }


def find_registered_transmitter(
    uuid: str,
    major: int,
    minor: int,
) -> RegisteredTransmitter | None:
    for transmitter in REGISTERED_TRANSMITTERS:
        if (
            transmitter.uuid.lower() == uuid.lower()
            and transmitter.major == major
            and transmitter.minor == minor
        ):
            return transmitter

    return None


# ------------------------------------------------------------
# Web管理画面用RSSIログ保存
# ------------------------------------------------------------

def save_web_rssi_log(
    transmitter: RegisteredTransmitter,
    device,
    rssi: int,
) -> None:
    mac_address = getattr(device, "address", "")

    if not mac_address:
        return

    connection = None

    try:
        connection = sqlite3.connect(
            WEB_DATABASE_PATH,
            timeout=5.0,
        )

        connection.execute(
            """
            INSERT INTO rssi_logs (
                mac_address,
                name,
                major,
                rssi,
                timestamp
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                mac_address,
                transmitter.rider_name,
                transmitter.major,
                rssi,
                time.time(),
            ),
        )

        connection.commit()

    except sqlite3.Error as error:
        print()
        print(
            f"[RSSIログ保存エラー] "
            f"{transmitter.serial_number}: {error}"
        )

    finally:
        if connection is not None:
            connection.close()


# ------------------------------------------------------------
# ラップ判定
# ------------------------------------------------------------

def record_lap(
    connection: sqlite3.Connection,
    transmitter: RegisteredTransmitter,
    state: TransmitterState,
    beacon: dict,
    device,
    advertisement_data,
) -> None:
    now_monotonic = time.monotonic()
    now_datetime = datetime.now()

    if state.last_lap_monotonic is not None:
        elapsed = now_monotonic - state.last_lap_monotonic

        if elapsed < MINIMUM_LAP_SECONDS:
            remaining = MINIMUM_LAP_SECONDS - elapsed

            print()
            print(
                f"[判定保留] {transmitter.serial_number} "
                f"最短ラップ時間まで残り {remaining:.1f} 秒"
            )
            return

    lap_time_seconds = None

    if state.last_lap_monotonic is not None:
        lap_time_seconds = now_monotonic - state.last_lap_monotonic

    state.lap_count += 1
    state.last_lap_monotonic = now_monotonic
    state.last_lap_datetime = now_datetime

    device_name = (
        advertisement_data.local_name
        or device.name
        or "(名前なし)"
    )

    save_lap(
        connection=connection,
        transmitter=transmitter,
        lap_number=state.lap_count,
        passed_at=now_datetime,
        lap_time_seconds=lap_time_seconds,
        rssi=advertisement_data.rssi,
        uuid=beacon["uuid"],
        major=beacon["major"],
        minor=beacon["minor"],
        device_address=device.address,
        device_name=device_name,
    )

    try:
        save_web_lap(
            name=transmitter.rider_name,
            major=transmitter.major,
            lap_number=state.lap_count,
            lap_time=lap_time_seconds,
        )
    except Exception as error:
        print()
        print(
            f"[Webラップ保存エラー] "
            f"{transmitter.serial_number}: {error}"
        )

    print()
    print()
    print("============================================")
    print("               ラップ検出")
    print("============================================")
    print(f"送信機     : {transmitter.serial_number}")
    print(f"ライダー   : {transmitter.rider_name}")
    print(f"車両       : {transmitter.bike_name}")
    print(f"ラップ番号 : {state.lap_count}")
    print(
        f"通過時刻   : "
        f"{now_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}"
    )

    if lap_time_seconds is None:
        print("ラップタイム: 初回通過")
    else:
        print(f"ラップタイム: {lap_time_seconds:.3f} 秒")

    print(f"RSSI       : {advertisement_data.rssi} dBm")
    print("============================================")
    print()


def process_beacon(
    connection: sqlite3.Connection,
    transmitter: RegisteredTransmitter,
    beacon: dict,
    device,
    advertisement_data,
) -> None:
    state = transmitter_states[transmitter.serial_number]
    rssi = advertisement_data.rssi

    if rssi is None:
        return

    state.last_rssi = rssi
    now_monotonic = time.monotonic()

    if state.waiting_for_clear_after_reset:
        if rssi < EXIT_RSSI_THRESHOLD:
            state.waiting_for_clear_after_reset = False
            state.inside_detection_zone = False

            print()
            print(
                f"[リセット後のゾーン離脱確認] "
                f"{transmitter.serial_number} RSSI={rssi} dBm"
            )
            print("次の通過から計測を開始します。")

        return

    if (
        now_monotonic - state.last_rssi_log_monotonic
        >= RSSI_LOG_INTERVAL_SECONDS
    ):
        save_web_rssi_log(
            transmitter=transmitter,
            device=device,
            rssi=rssi,
        )

        state.last_rssi_log_monotonic = now_monotonic

    if (
        now_monotonic - state.last_display_monotonic
        >= DISPLAY_INTERVAL_SECONDS
    ):
        zone_text = (
            "通過済み"
            if state.inside_detection_zone
            else "待機中"
        )

        print(
            f"\r"
            f"{transmitter.serial_number}  "
            f"RSSI={rssi:4d} dBm  "
            f"状態={zone_text:8s}  "
            f"ラップ={state.lap_count:3d}",
            end="",
            flush=True,
        )

        state.last_display_monotonic = now_monotonic

    if not state.inside_detection_zone:
        state.exit_candidate_since = None

        if rssi >= ENTRY_RSSI_THRESHOLD:
            if state.entry_candidate_since is None:
                state.entry_candidate_since = now_monotonic

            entry_elapsed = now_monotonic - state.entry_candidate_since

            if entry_elapsed >= ENTRY_CONFIRM_SECONDS:
                state.inside_detection_zone = True
                state.entry_candidate_since = None

                record_lap(
                    connection=connection,
                    transmitter=transmitter,
                    state=state,
                    beacon=beacon,
                    device=device,
                    advertisement_data=advertisement_data,
                )
        else:
            state.entry_candidate_since = None

        return

    state.entry_candidate_since = None

    if rssi < EXIT_RSSI_THRESHOLD:
        if state.exit_candidate_since is None:
            state.exit_candidate_since = now_monotonic

        exit_elapsed = now_monotonic - state.exit_candidate_since

        if exit_elapsed >= EXIT_CONFIRM_SECONDS:
            state.inside_detection_zone = False
            state.exit_candidate_since = None

            print()
            print(
                f"[ゾーン離脱] {transmitter.serial_number} "
                f"RSSI={rssi} dBm"
            )
            print("次の通過を受け付けます。")
    else:
        state.exit_candidate_since = None


# ------------------------------------------------------------
# Liteモード練習リセット処理
# ------------------------------------------------------------

def process_lite_reset_request(
    connection: sqlite3.Connection,
) -> None:
    if not RESET_REQUEST_PATH.exists():
        return

    connection.execute("DELETE FROM laps")
    connection.commit()

    for state in transmitter_states.values():
        state.inside_detection_zone = True
        state.last_lap_monotonic = None
        state.last_lap_datetime = None
        state.lap_count = 0
        state.last_display_monotonic = 0.0
        state.last_rssi = None
        state.waiting_for_clear_after_reset = True
        state.entry_candidate_since = None
        state.exit_candidate_since = None

    try:
        RESET_REQUEST_PATH.unlink()
    except FileNotFoundError:
        pass

    print()
    print("============================================")
    print(" Liteモード練習データをリセットしました")
    print("============================================")


# ------------------------------------------------------------
# 省電力送信機を起こすためのBLE接続
# ------------------------------------------------------------

async def connect_to_transmitter_once(
    transmitter: RegisteredTransmitter,
    device,
) -> None:
    serial_number = transmitter.serial_number
    address = device.address

    try:
        print()
        print(
            f"[BLE接続開始] {serial_number} "
            f"address={address}"
        )

        async with BleakClient(address, timeout=8.0) as client:
            if client.is_connected:
                connected_serial_numbers.add(serial_number)

                print(
                    f"[BLE接続成功] {serial_number} "
                    f"{KEEP_ALIVE_CONNECT_SECONDS:.0f}秒間接続を維持します。"
                )

                course_mode_until_monotonic[serial_number] = (
                    time.monotonic() + COURSE_MODE_SKIP_SECONDS
                )

                print(
                    f"[コース内モード扱い] {serial_number} "
                    "4時間は再接続しません。"
                )

                await asyncio.sleep(KEEP_ALIVE_CONNECT_SECONDS)

    except Exception as error:
        course_mode_until_monotonic[serial_number] = (
            time.monotonic() + SEEN_SKIP_SECONDS
        )

        print()
        print(
            f"[BLE接続失敗] {serial_number}: {error}"
        )
        print(
            f"[再接続抑制] {serial_number} "
            "RSSI受信済みのため10分間は再接続しません。"
        )

    finally:
        connected_serial_numbers.discard(serial_number)
        connecting_serial_numbers.discard(serial_number)

        print()
        print(
            f"[BLE接続終了] {serial_number}"
        )


# ------------------------------------------------------------
# BLE受信
# ------------------------------------------------------------

def request_ble_connection_if_needed(
    transmitter: RegisteredTransmitter,
    device,
    rssi: int | None,
) -> None:
    serial_number = transmitter.serial_number

    if rssi is None:
        return

    if rssi < CONNECT_MIN_RSSI:
        return

    now_monotonic = time.monotonic()

    course_mode_until = course_mode_until_monotonic.get(
        serial_number,
        0.0,
    )

    if now_monotonic < course_mode_until:
        return

    if serial_number in connected_serial_numbers:
        return

    if serial_number in connecting_serial_numbers:
        return

    last_attempt = last_connect_attempt_monotonic.get(
        serial_number,
        0.0,
    )

    if (
        now_monotonic - last_attempt
        < CONNECT_RETRY_INTERVAL_SECONDS
    ):
        return

    last_connect_attempt_monotonic[serial_number] = now_monotonic
    connecting_serial_numbers.add(serial_number)
    pending_connect_requests[serial_number] = (
        transmitter,
        device,
    )


def create_detection_callback(connection: sqlite3.Connection):
    def detection_callback(device, advertisement_data):
        for company_id, manufacturer_data in (
            advertisement_data.manufacturer_data.items()
        ):
            beacon = parse_ibeacon(
                company_id,
                manufacturer_data,
            )

            if beacon is None:
                continue

            transmitter = find_registered_transmitter(
                uuid=beacon["uuid"],
                major=beacon["major"],
                minor=beacon["minor"],
            )

            if transmitter is None:
                continue

            request_ble_connection_if_needed(
                transmitter=transmitter,
                device=device,
                rssi=advertisement_data.rssi,
            )

            process_beacon(
                connection=connection,
                transmitter=transmitter,
                beacon=beacon,
                device=device,
                advertisement_data=advertisement_data,
            )

    return detection_callback


# ------------------------------------------------------------
# 終了処理
# ------------------------------------------------------------

def stop_program(signum, frame):
    global running

    print()
    print("終了要求を受け付けました。")
    running = False


# ------------------------------------------------------------
# メイン
# ------------------------------------------------------------

async def main():
    signal.signal(signal.SIGINT, stop_program)
    signal.signal(signal.SIGTERM, stop_program)

    connection = open_database()
    register_transmitters(connection)
    load_existing_lap_counts(connection)

    print("============================================")
    print("Raspberry Pi 5 BLEラップタイマー")
    print("============================================")
    print(f"データベース   : {DATABASE_PATH}")
    print(f"通過判定RSSI   : {ENTRY_RSSI_THRESHOLD} dBm以上")
    print(f"ゾーン離脱RSSI : {EXIT_RSSI_THRESHOLD} dBm未満")
    print(f"最短ラップ時間 : {MINIMUM_LAP_SECONDS:.1f}秒")
    print()
    print("登録送信機:")

    for transmitter in REGISTERED_TRANSMITTERS:
        state = transmitter_states[transmitter.serial_number]

        print(
            f"  {transmitter.serial_number} "
            f"Major={transmitter.major} "
            f"Minor={transmitter.minor} "
            f"保存済みラップ={state.lap_count}"
        )

    print()
    print("BLEスキャンを開始します。")
    print("終了する場合は Ctrl + C を押してください。")
    print()

    callback = create_detection_callback(connection)

    try:
        while running:
            process_lite_reset_request(connection)
            scanner = BleakScanner(callback)

            try:
                await scanner.start()
                scan_started = time.monotonic()

                while (
                    running
                    and time.monotonic() - scan_started
                    < SCAN_RESTART_SECONDS
                ):
                    process_lite_reset_request(connection)

                    if pending_connect_requests:
                        serial_number, request = next(
                            iter(pending_connect_requests.items())
                        )

                        transmitter_to_connect, device_to_connect = request

                        pending_connect_requests.pop(
                            serial_number,
                            None,
                        )

                        try:
                            await scanner.stop()
                        except Exception:
                            pass

                        await connect_to_transmitter_once(
                            transmitter=transmitter_to_connect,
                            device=device_to_connect,
                        )

                        break

                    await asyncio.sleep(0.2)

            except Exception as error:
                print()
                print(f"BLEスキャンエラー: {error}")
                print("3秒後に再試行します。")
                await asyncio.sleep(3)

            finally:
                try:
                    await scanner.stop()
                except Exception:
                    pass

    finally:
        connection.close()

        print()
        print("BLEラップタイマーを終了しました。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        print("キーボード割り込みで終了しました。")
    except Exception as error:
        print()
        print(f"予期しないエラー: {error}")