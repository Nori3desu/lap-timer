import asyncio
from datetime import datetime

from bleak import BleakScanner


# ==================================================
# ESP32-C3 iBeacon 受信確認用
# ==================================================

TARGET_UUID = "12345678-1234-1234-1234-123456789abc"
TARGET_MAJOR = 1
TARGET_MINOR = 1

SCAN_SECONDS = 30


def format_uuid(uuid_bytes: bytes) -> str:
    """16バイトのUUIDを通常の文字列形式に変換する。"""
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
    Bleakのmanufacturer_dataからiBeacon情報を取り出す。

    company_id:
        Apple = 0x004C

    data:
        02 15
        UUID 16バイト
        Major 2バイト
        Minor 2バイト
        TX Power 1バイト
    """

    if company_id != 0x004C:
        return None

    if len(data) < 23:
        return None

    if data[0:2] != b"\x02\x15":
        return None

    uuid_value = format_uuid(data[2:18])
    major = int.from_bytes(data[18:20], byteorder="big")
    minor = int.from_bytes(data[20:22], byteorder="big")
    tx_power = int.from_bytes(data[22:23], byteorder="big", signed=True)

    return {
        "uuid": uuid_value,
        "major": major,
        "minor": minor,
        "tx_power": tx_power,
    }


def detection_callback(device, advertisement_data):
    """BLE広告を受信するたびに呼ばれる。"""

    for company_id, manufacturer_data in advertisement_data.manufacturer_data.items():
        beacon = parse_ibeacon(company_id, manufacturer_data)

        if beacon is None:
            continue

        if (
            beacon["uuid"].lower() == TARGET_UUID.lower()
            and beacon["major"] == TARGET_MAJOR
            and beacon["minor"] == TARGET_MINOR
        ):
            now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            name = advertisement_data.local_name or device.name or "(名前なし)"

            print()
            print("========== 受信成功 ==========")
            print(f"時刻    : {now}")
            print(f"名前    : {name}")
            print(f"アドレス: {device.address}")
            print(f"RSSI    : {advertisement_data.rssi} dBm")
            print(f"UUID    : {beacon['uuid']}")
            print(f"Major   : {beacon['major']}")
            print(f"Minor   : {beacon['minor']}")
            print(f"TX Power: {beacon['tx_power']} dBm")
            print("==============================")


async def main():
    print("ESP32-C3のiBeaconを探します。")
    print(f"対象UUID : {TARGET_UUID}")
    print(f"対象Major: {TARGET_MAJOR}")
    print(f"対象Minor: {TARGET_MINOR}")
    print(f"検索時間 : {SCAN_SECONDS}秒")
    print()

    scanner = BleakScanner(detection_callback)

    try:
        await scanner.start()
        await asyncio.sleep(SCAN_SECONDS)
    finally:
        await scanner.stop()

    print()
    print("BLEスキャンを終了しました。")


if __name__ == "__main__":
    asyncio.run(main())