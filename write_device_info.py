import asyncio

from bleak import BleakScanner
from bleak import BleakClient

NAME_CHAR_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
MODEL_CHAR_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TEAM_CHAR_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"

TARGET_MAC = "1C:C3:AB:E5:46:A6"


async def main():
    print("SCAN START")

    devices = await BleakScanner.discover(timeout=10.0)

    target = None

    for device in devices:
        print(device.address, device.name)

        if device.address == TARGET_MAC:
            target = device

    if target is None:
        print("TARGET NOT FOUND")
        return

    print("CONNECT", target.address)

    async with BleakClient(target) as client:
        print("CONNECTED")

        await client.write_gatt_char(NAME_CHAR_UUID, "TARO".encode())
        await client.write_gatt_char(MODEL_CHAR_UUID, "YZ125".encode())
        await client.write_gatt_char(TEAM_CHAR_UUID, "TEAM A".encode())

        print("WRITE DONE")


asyncio.run(main())