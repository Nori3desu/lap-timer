import asyncio
from datetime import datetime

from bleak import BleakScanner


SCAN_SECONDS = 30


def format_manufacturer_data(
    manufacturer_data: dict[int, bytes],
) -> str:
    if not manufacturer_data:
        return "なし"

    lines: list[str] = []

    for company_id, data in manufacturer_data.items():
        lines.append(
            f"Company ID 0x{company_id:04X}: "
            f"{data.hex()}"
        )

    return "\n                   ".join(lines)


async def main() -> None:
    print("周囲のBLE機器をすべて検索します。")
    print(f"検索時間: {SCAN_SECONDS}秒")
    print()

    seen: dict[str, tuple[str, int | None]] = {}

    def callback(device, advertisement_data) -> None:
        name = (
            advertisement_data.local_name
            or device.name
            or "名前なし"
        )

        address = device.address
        rssi = advertisement_data.rssi

        manufacturer_text = format_manufacturer_data(
            advertisement_data.manufacturer_data
        )

        current = (name, rssi)

        if seen.get(address) == current:
            return

        seen[address] = current

        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        print("----------------------------------------")
        print(f"時刻              : {now}")
        print(f"名前              : {name}")
        print(f"アドレス          : {address}")
        print(f"RSSI              : {rssi} dBm")
        print(
            "Manufacturer Data : "
            f"{manufacturer_text}"
        )

        if advertisement_data.service_uuids:
            print(
                "Service UUIDs     : "
                + ", ".join(
                    advertisement_data.service_uuids
                )
            )

    scanner = BleakScanner(
        detection_callback=callback
    )

    await scanner.start()

    try:
        await asyncio.sleep(SCAN_SECONDS)
    finally:
        await scanner.stop()

    print()
    print("BLEスキャンを終了しました。")
    print(f"検出したアドレス数: {len(seen)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        print("中止しました。")