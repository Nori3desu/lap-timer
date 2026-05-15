import asyncio
import traceback

from bleak import BleakScanner
from bleak import BleakClient

from database import (
    init_db,
    upsert_device,
)

NAME_CHAR_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
MODEL_CHAR_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TEAM_CHAR_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"

TARGET_DEVICE_NAME = "LapTimer"


async def register_device(device):
    mac_address = device.address

    print()
    print(f"CONNECT {mac_address}")

    try:
        async with BleakClient(device) as client:
            print("CONNECTED")

            services = client.services

            print("SERVICES:")
            for service in services:
                print(f"  SERVICE {service.uuid}")

                for char in service.characteristics:
                    print(f"    CHAR {char.uuid} {char.properties}")

            print("READ CHARACTERISTICS")

            name_bytes = await client.read_gatt_char(NAME_CHAR_UUID)
            model_bytes = await client.read_gatt_char(MODEL_CHAR_UUID)
            team_bytes = await client.read_gatt_char(TEAM_CHAR_UUID)

            name = name_bytes.decode()
            model_name = model_bytes.decode()
            team_name = team_bytes.decode()

            minor = upsert_device(
                mac_address,
                name,
                model_name,
                team_name
            )

            print()
            print("REGISTERED")
            print(f"mac        : {mac_address}")
            print(f"minor      : {minor}")
            print(f"name       : {name}")
            print(f"model_name : {model_name}")
            print(f"team_name  : {team_name}")

    except Exception as e:
        print()
        print(f"ERROR {mac_address}")
        print("TYPE:", type(e))
        print("REPR:", repr(e))
        print("TRACEBACK:")
        traceback.print_exc()


async def main():
    init_db()

    print("SCAN START")

    devices = await BleakScanner.discover(timeout=10.0)

    found = False

    for device in devices:
        print(device.address, device.name)

        if device.name != TARGET_DEVICE_NAME:
            continue

        found = True

        await register_device(device)

    if not found:
        print()
        print("NO TARGET DEVICE FOUND")


asyncio.run(main())