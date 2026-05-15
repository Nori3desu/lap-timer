import asyncio

from bleak import BleakScanner
from bleak import BleakClient

NAME_CHAR_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
MODEL_CHAR_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
TEAM_CHAR_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


async def find_device_by_mac(mac_address):
    devices = await BleakScanner.discover(timeout=10.0)

    for device in devices:
        if device.address == mac_address:
            return device

    return None


async def write_device_info(mac_address, name, model_name, team_name):
    device = await find_device_by_mac(mac_address)

    if device is None:
        raise RuntimeError("ESP32が見つかりません。電源や登録モードを確認してください。")

    async with BleakClient(device) as client:
        await client.write_gatt_char(NAME_CHAR_UUID, name.encode())
        await client.write_gatt_char(MODEL_CHAR_UUID, model_name.encode())
        await client.write_gatt_char(TEAM_CHAR_UUID, team_name.encode())