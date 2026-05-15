import asyncio

from bleak import BleakScanner, BleakClient

from database import upsert_device

NAME_CHAR_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
MODEL_CHAR_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TEAM_CHAR_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"

TARGET_DEVICE_NAME = "LapTimer"


async def scan_and_register_one():
    devices = await BleakScanner.discover(timeout=10.0)

    for device in devices:
        if device.name != TARGET_DEVICE_NAME:
            continue

        mac_address = device.address

        try:
            async with BleakClient(device, timeout=20.0) as client:
                name_bytes = await client.read_gatt_char(NAME_CHAR_UUID)
                model_bytes = await client.read_gatt_char(MODEL_CHAR_UUID)
                team_bytes = await client.read_gatt_char(TEAM_CHAR_UUID)

                name = name_bytes.decode()
                model_name = model_bytes.decode()
                team_name = team_bytes.decode()

                upsert_device(
                    mac_address,
                    name,
                    model_name,
                    team_name,
                    1
                )

                return mac_address

        except Exception as e:
            print("ENTRY CONNECT ERROR:", mac_address, repr(e))
            continue

    return None


def scan_and_register_sync():
    return asyncio.run(scan_and_register_one())