import asyncio
import sqlite3
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bleak import BleakScanner


# ==========================================================
# Raspberry Pi 5 BLEラップタイマー受信機
#
# 機能:
#   ・登録済みiBeaconのみを識別
#   ・RSSIをリアルタイム表示
#   ・受信イベントをSQLiteへ保存
#
# 現段階ではラップ確定処理は行わない
# ==========================================================


# ----------------------------------------------------------
# 基本設定
# ----------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "lap_timer.sqlite3"

# BLEスキャンを何秒ごとに区切るか
SCAN_RESTART_SECONDS = 60

# 同じ送信機のDB記録間隔
# BLE広告は約100msごとに届くため、全部保存すると件数が増えすぎる
DATABASE_SAVE_INTERVAL_SECONDS = 1.0

# これより弱い電波は画面表示・保存の対象外
# 最初のテストでは -85程度にして広めに受信する
MIN_RSSI = -85


# ----------------------------------------------------------
# 登録送信機
# ----------------------------------------------------------

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
        uuid="bc9a7856-3412-3412-3412-341278563412",
        major=1,
        minor=1,
        rider_name="テストライダー1",
        bike_name="テスト車両1",
    ),
]


# ----------------------------------------------------------
# 実行状態
# ----------------------------------------------------------

running = True

# 送信機ごとの最終保存時刻
last_saved_times: dict[str, float] = {}

# 受信回数
received_counts: dict[str, int] = {}


# ----------------------------------------------------------
# SQLite
# ----------------------------------------------------------

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
        CREATE TABLE IF NOT EXISTS ble_receptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial_number TEXT NOT NULL,
            received_at TEXT NOT NULL,
            monotonic_time REAL NOT NULL,
            device_address TEXT,
            device_name TEXT,
            rssi INTEGER NOT NULL,
            uuid TEXT NOT NULL,
            major INTEGER NOT NULL,
            minor INTEGER NOT NULL,
            tx_power INTEGER,
            FOREIGN KEY(serial_number)
                REFERENCES transmitters(serial_number)
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_ble_receptions_serial_time
        ON ble_receptions(serial_number, received_at)
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


def save_reception(
    connection: sqlite3.Connection,
    transmitter: RegisteredTransmitter,
    received_at: str,
    monotonic_time: float,
    device_address: str,
    device_name: str,
    rssi: int,
    uuid: str,
    major: int,
    minor: int,
    tx_power: int,
) -> None:
    connection.execute(
        """
        INSERT INTO ble_receptions (
            serial_number,
            received_at,
            monotonic_time,
            device_address,
            device_name,
            rssi,
            uuid,
            major,
            minor,
            tx_power
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transmitter.serial_number,
            received_at,
            monotonic_time,
            device_address,
            device_name,
            rssi,
            uuid,
            major,
            minor,
            tx_power,
        ),
    )

    connection.commit()


# ----------------------------------------------------------
# iBeacon解析
# ----------------------------------------------------------

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
    """
    Bleakのmanufacturer_dataを解析する。

    Apple Company ID:
        0x004C

    iBeaconデータ:
        02 15
        UUID     16バイト
        Major     2バイト
        Minor     2バイト
        TX Power  1バイト
    """

    if company_id != 0x004C:
        return None

    if len(data) < 23:
        return None

    if data[0:2] != b"\x02\x15":
        return None

    uuid = format_uuid(data[2:18]).lower()
    major = int.from_bytes(data[18:20], byteorder="big")
    minor = int.from_bytes(data[20:22], byteorder="big")
    tx_power = int.from_bytes(
        data[22:23],
        byteorder="big",
        signed=True,
    )

    return {
        "uuid": uuid,
        "major": major,
        "minor": minor,
        "tx_power": tx_power,
    }


def find_registered_transmitter(
    uuid: str,
    major: int,
    minor: int,
):
    for transmitter in REGISTERED_TRANSMITTERS:
        if (
            transmitter.uuid.lower() == uuid.lower()
            and transmitter.major == major
            and transmitter.minor == minor
        ):
            return transmitter

    return None


# ----------------------------------------------------------
# BLE受信処理
# ----------------------------------------------------------

def create_detection_callback(connection: sqlite3.Connection):
    def detection_callback(device, advertisement_data):
        rssi = advertisement_data.rssi

        if rssi is None:
            return

        if rssi < MIN_RSSI:
            return

        for company_id, manufacturer_data in (
            advertisement_data.manufacturer_data.items()
        ):
            beacon = parse_ibeacon(company_id, manufacturer_data)

            if beacon is None:
                continue

            transmitter = find_registered_transmitter(
                uuid=beacon["uuid"],
                major=beacon["major"],
                minor=beacon["minor"],
            )

            # 登録されていない送信機は無視
            if transmitter is None:
                continue

            now_monotonic = time.monotonic()
            received_at = datetime.now().isoformat(timespec="milliseconds")

            device_name = (
                advertisement_data.local_name
                or device.name
                or "(名前なし)"
            )

            received_counts[transmitter.serial_number] = (
                received_counts.get(transmitter.serial_number, 0) + 1
            )

            count = received_counts[transmitter.serial_number]

            print(
                f"\r"
                f"{received_at}  "
                f"{transmitter.serial_number}  "
                f"Major={beacon['major']}  "
                f"Minor={beacon['minor']}  "
                f"RSSI={rssi:4d} dBm  "
                f"受信={count:7d}回",
                end="",
                flush=True,
            )

            last_saved = last_saved_times.get(
                transmitter.serial_number,
                0.0,
            )

            if (
                now_monotonic - last_saved
                < DATABASE_SAVE_INTERVAL_SECONDS
            ):
                continue

            save_reception(
                connection=connection,
                transmitter=transmitter,
                received_at=received_at,
                monotonic_time=now_monotonic,
                device_address=device.address,
                device_name=device_name,
                rssi=rssi,
                uuid=beacon["uuid"],
                major=beacon["major"],
                minor=beacon["minor"],
                tx_power=beacon["tx_power"],
            )

            last_saved_times[transmitter.serial_number] = now_monotonic

    return detection_callback


# ----------------------------------------------------------
# 終了処理
# ----------------------------------------------------------

def stop_program(signum, frame):
    global running

    print()
    print("終了要求を受け付けました。")
    running = False


# ----------------------------------------------------------
# メイン処理
# ----------------------------------------------------------

async def main():
    signal.signal(signal.SIGINT, stop_program)
    signal.signal(signal.SIGTERM, stop_program)

    connection = open_database()
    register_transmitters(connection)

    print("============================================")
    print("Raspberry Pi 5 BLE受信機")
    print("============================================")
    print(f"データベース : {DATABASE_PATH}")
    print(f"最低RSSI     : {MIN_RSSI} dBm")
    print()
    print("登録送信機:")

    for transmitter in REGISTERED_TRANSMITTERS:
        print(
            f"  {transmitter.serial_number} "
            f"UUID={transmitter.uuid} "
            f"Major={transmitter.major} "
            f"Minor={transmitter.minor}"
        )

    print()
    print("BLEスキャンを開始します。")
    print("終了する場合は Ctrl + C を押してください。")
    print()

    detection_callback = create_detection_callback(connection)

    try:
        while running:
            scanner = BleakScanner(detection_callback)

            try:
                await scanner.start()

                start_time = time.monotonic()

                while (
                    running
                    and time.monotonic() - start_time
                    < SCAN_RESTART_SECONDS
                ):
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
        print("BLE受信機を終了しました。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print()
        print(f"予期しないエラー: {error}")
        sys.exit(1)
