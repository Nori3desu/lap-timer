from bleak import BleakScanner
import asyncio
import struct

from lap_manager import LapManager
from database import init_db, get_device_by_mac

TARGET_UUID = "ab907856-3412-3412-3412-341278563412"

lap_manager = LapManager()


def parse_ibeacon(advertisement_data):
    manufacturer_data = advertisement_data.manufacturer_data

    if 76 not in manufacturer_data:
        return None

    data = manufacturer_data[76]

    if len(data) < 23:
        return None

    uuid_bytes = data[2:18]
    uuid_hex = uuid_bytes.hex()

    uuid = (
        f"{uuid_hex[0:8]}-"
        f"{uuid_hex[8:12]}-"
        f"{uuid_hex[12:16]}-"
        f"{uuid_hex[16:20]}-"
        f"{uuid_hex[20:32]}"
    )

    major = struct.unpack(">H", data[18:20])[0]
    minor = struct.unpack(">H", data[20:22])[0]

    return {
        "uuid": uuid.lower(),
        "major": major,
        "minor": minor,
    }


def detection_callback(device, advertisement_data):
    beacon = parse_ibeacon(advertisement_data)

    if beacon is None:
        return

    if beacon["uuid"] != TARGET_UUID:
        return

    mac_address = device.address

    device_info = get_device_by_mac(mac_address)

    if device_info is None:
        print(f"UNREGISTERED DEVICE {mac_address}")
        return

    name = device_info["name"]
    major = device_info["major"]
    rssi = advertisement_data.rssi

    lap_manager.update(
        name,
        major,
        rssi
    )


async def main():
    init_db()

    print("BLE LAP TIMER START")
    print("UUID filter enabled")
    print("Device identification: BLE MAC address")
    print("Press Ctrl+C to stop")

    scanner = BleakScanner(detection_callback)

    await scanner.start()

    while True:
        await asyncio.sleep(1)


asyncio.run(main())