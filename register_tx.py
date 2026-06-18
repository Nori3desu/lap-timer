import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice


# ==================================================
# BLE送信機情報読出し・DB自動登録
# ==================================================

TARGET_NAME = "TX-0001"

DB_PATH = Path(__file__).resolve().parent / "lap_timer.db"

INFO_SERVICE_UUID = (
    "7d2a0001-7c11-4a72-8f2d-4c1d5a6b0001"
)

CHARACTERISTICS = {
    "device_id": (
        "7d2a0002-7c11-4a72-8f2d-4c1d5a6b0001"
    ),
    "rider_name": (
        "7d2a0003-7c11-4a72-8f2d-4c1d5a6b0001"
    ),
    "model_name": (
        "7d2a0004-7c11-4a72-8f2d-4c1d5a6b0001"
    ),
    "team_name": (
        "7d2a0005-7c11-4a72-8f2d-4c1d5a6b0001"
    ),
    "major": (
        "7d2a0006-7c11-4a72-8f2d-4c1d5a6b0001"
    ),
    "minor": (
        "7d2a0007-7c11-4a72-8f2d-4c1d5a6b0001"
    ),
    "data_version": (
        "7d2a0008-7c11-4a72-8f2d-4c1d5a6b0001"
    ),
}


def decode_value(data: bytearray) -> str:
    """BLEから読み取ったUTF-8データを文字列へ変換する。"""
    return bytes(data).decode(
        "utf-8",
        errors="replace",
    ).strip()


async def find_transmitter(
    timeout: float = 15.0,
) -> Optional[BLEDevice]:
    """指定されたBLE名の送信機を探す。"""

    print(f"{TARGET_NAME}を探しています...")

    found_device: Optional[BLEDevice] = None
    found_event = asyncio.Event()

    def detection_callback(
        device: BLEDevice,
        advertisement_data,
    ) -> None:
        nonlocal found_device

        local_name = advertisement_data.local_name

        if local_name == TARGET_NAME:
            found_device = device
            found_event.set()

    scanner = BleakScanner(
        detection_callback=detection_callback
    )

    await scanner.start()

    try:
        await asyncio.wait_for(
            found_event.wait(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return None
    finally:
        await scanner.stop()

    return found_device


async def read_transmitter_info(
    device: BLEDevice,
) -> dict[str, str]:
    """送信機へ接続して保存情報を読み取る。"""

    print()
    print("BLE接続を開始します...")

    async with BleakClient(
        device,
        timeout=20.0,
    ) as client:
        if not client.is_connected:
            raise RuntimeError("BLE接続に失敗しました。")

        print("BLE接続に成功しました。")

        services = client.services

        if services.get_service(INFO_SERVICE_UUID) is None:
            raise RuntimeError(
                "送信機情報サービスが見つかりません。"
            )

        values: dict[str, str] = {}

        for key, characteristic_uuid in (
            CHARACTERISTICS.items()
        ):
            raw_value = await client.read_gatt_char(
                characteristic_uuid
            )

            values[key] = decode_value(raw_value)

        return values


def validate_transmitter_info(
    values: dict[str, str],
) -> None:
    """DB登録前に必須項目を確認する。"""

    required_fields = {
        "device_id": values.get("device_id", ""),
        "rider_name": values.get("rider_name", ""),
        "major": values.get("major", ""),
        "minor": values.get("minor", ""),
    }

    missing_fields = [
        key
        for key, value in required_fields.items()
        if not value
    ]

    if missing_fields:
        raise ValueError(
            "必須情報が空です: "
            + ", ".join(missing_fields)
        )

    try:
        major = int(values["major"])
        minor = int(values["minor"])
    except ValueError as error:
        raise ValueError(
            "majorまたはminorが整数ではありません。"
        ) from error

    if not 0 <= major <= 65535:
        raise ValueError(
            f"majorが範囲外です: {major}"
        )

    if not 0 <= minor <= 65535:
        raise ValueError(
            f"minorが範囲外です: {minor}"
        )


def register_device(
    mac_address: str,
    values: dict[str, str],
) -> str:
    """
    devicesテーブルへ登録する。

    未登録：
        INSERT

    同じMACアドレスが登録済み：
        UPDATE

    同じminorが別MACアドレスで登録済み：
        エラー
    """

    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"DBファイルが見つかりません: {DB_PATH}"
        )

    current_time = time.time()

    major = int(values["major"])
    minor = int(values["minor"])

    rider_name = values["rider_name"]
    model_name = values.get("model_name", "")
    team_name = values.get("team_name", "")

    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row

        # 同じminorが別のBLEアドレスに割り当てられていないか確認
        duplicate_minor = connection.execute(
            """
            SELECT
                mac_address,
                minor,
                name
            FROM devices
            WHERE minor = ?
              AND mac_address <> ?
            """,
            (
                minor,
                mac_address,
            ),
        ).fetchone()

        if duplicate_minor is not None:
            raise ValueError(
                "同じminorが別の送信機に登録されています。\n"
                f"minor       : {minor}\n"
                f"登録済みMAC : "
                f"{duplicate_minor['mac_address']}\n"
                f"登録名      : "
                f"{duplicate_minor['name']}"
            )

        existing_device = connection.execute(
            """
            SELECT mac_address
            FROM devices
            WHERE mac_address = ?
            """,
            (mac_address,),
        ).fetchone()

        if existing_device is None:
            connection.execute(
                """
                INSERT INTO devices (
                    mac_address,
                    minor,
                    name,
                    model_name,
                    team_name,
                    created_at,
                    updated_at,
                    major
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mac_address,
                    minor,
                    rider_name,
                    model_name,
                    team_name,
                    current_time,
                    current_time,
                    major,
                ),
            )

            action = "新規登録"

        else:
            connection.execute(
                """
                UPDATE devices
                SET
                    minor = ?,
                    name = ?,
                    model_name = ?,
                    team_name = ?,
                    updated_at = ?,
                    major = ?
                WHERE mac_address = ?
                """,
                (
                    minor,
                    rider_name,
                    model_name,
                    team_name,
                    current_time,
                    major,
                    mac_address,
                ),
            )

            action = "登録更新"

        connection.commit()

    return action


def print_transmitter_info(
    device: BLEDevice,
    values: dict[str, str],
) -> None:
    """読み取った送信機情報を表示する。"""

    print()
    print("================================")
    print(" 送信機から読み取った情報")
    print("================================")
    print(f"BLEアドレス  : {device.address}")
    print(f"device_id    : {values['device_id']}")
    print(f"rider_name   : {values['rider_name']}")
    print(f"model_name   : {values['model_name']}")
    print(f"team_name    : {values['team_name']}")
    print(f"major        : {values['major']}")
    print(f"minor        : {values['minor']}")
    print(f"data_version : {values['data_version']}")
    print("================================")


def print_registered_device(
    mac_address: str,
) -> None:
    """DBへ登録された内容を表示する。"""

    with sqlite3.connect(DB_PATH) as connection:
        connection.row_factory = sqlite3.Row

        row = connection.execute(
            """
            SELECT
                mac_address,
                major,
                minor,
                name,
                model_name,
                team_name,
                created_at,
                updated_at
            FROM devices
            WHERE mac_address = ?
            """,
            (mac_address,),
        ).fetchone()

    if row is None:
        print("DB登録内容を確認できませんでした。")
        return

    print()
    print("================================")
    print(" devicesテーブル登録内容")
    print("================================")
    print(f"mac_address : {row['mac_address']}")
    print(f"major       : {row['major']}")
    print(f"minor       : {row['minor']}")
    print(f"name        : {row['name']}")
    print(f"model_name  : {row['model_name']}")
    print(f"team_name   : {row['team_name']}")
    print(f"created_at  : {row['created_at']}")
    print(f"updated_at  : {row['updated_at']}")
    print("================================")


async def main() -> None:
    device = await find_transmitter()

    if device is None:
        print()
        print(f"{TARGET_NAME}が見つかりませんでした。")
        print(
            "TX-0001をPi 5の近くへ置き、"
            "送信機を起動して再実行してください。"
        )
        return

    print()
    print("送信機を検出しました。")
    print(f"名前       : {device.name}")
    print(f"アドレス   : {device.address}")

    values = await read_transmitter_info(device)

    validate_transmitter_info(values)

    print_transmitter_info(
        device,
        values,
    )

    action = register_device(
        mac_address=device.address,
        values=values,
    )

    print()
    print(f"DBへの{action}が完了しました。")

    print_registered_device(
        device.address
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print()
        print("処理を中止しました。")

    except Exception as error:
        print()
        print("エラーが発生しました。")
        print(f"内容: {error}")