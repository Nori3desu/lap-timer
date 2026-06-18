import asyncio
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice


# ==================================================
# TX-0001 ユーザー情報読出し確認プログラム
# ==================================================

TARGET_NAME = "TX-0001"

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
    """BLEから受信したUTF-8データを文字列へ変換する。"""
    return bytes(data).decode(
        "utf-8",
        errors="replace",
    )


async def find_transmitter(
    timeout: float = 15.0,
) -> Optional[BLEDevice]:
    """TX-0001を名前で探す。"""

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


async def read_transmitter_info() -> None:
    device = await find_transmitter()

    if device is None:
        print()
        print(f"{TARGET_NAME}が見つかりませんでした。")
        print("送信機をPi 5の近くへ置いて再実行してください。")
        return

    print()
    print("送信機を検出しました。")
    print(f"名前      : {device.name}")
    print(f"アドレス  : {device.address}")
    print()
    print("BLE接続を開始します...")

    try:
        async with BleakClient(
            device,
            timeout=20.0,
        ) as client:
            if not client.is_connected:
                print("BLE接続に失敗しました。")
                return

            print("BLE接続に成功しました。")
            print()

            services = client.services

            if services.get_service(
                INFO_SERVICE_UUID
            ) is None:
                print(
                    "TX情報サービスが見つかりません。"
                )
                return

            values: dict[str, str] = {}

            for key, characteristic_uuid in (
                CHARACTERISTICS.items()
            ):
                raw_value = (
                    await client.read_gatt_char(
                        characteristic_uuid
                    )
                )

                values[key] = decode_value(
                    raw_value
                )

            print("================================")
            print(" TX-0001 読出し結果")
            print("================================")

            print(
                f"device_id    : "
                f"{values['device_id']}"
            )

            print(
                f"rider_name   : "
                f"{values['rider_name']}"
            )

            print(
                f"model_name   : "
                f"{values['model_name']}"
            )

            print(
                f"team_name    : "
                f"{values['team_name']}"
            )

            print(
                f"major        : "
                f"{values['major']}"
            )

            print(
                f"minor        : "
                f"{values['minor']}"
            )

            print(
                f"data_version : "
                f"{values['data_version']}"
            )

            print("================================")

    except Exception as error:
        print()
        print("BLE通信中にエラーが発生しました。")
        print(f"内容: {error}")


if __name__ == "__main__":
    try:
        asyncio.run(
            read_transmitter_info()
        )
    except KeyboardInterrupt:
        print()
        print("処理を中止しました。")
