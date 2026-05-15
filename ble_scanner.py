from bleak import BleakScanner
import asyncio

class BLEScanner:

    def __init__(self, callback):
        self.callback = callback

    def detection_callback(self, device, advertisement_data):

        rssi = advertisement_data.rssi
        name = device.name

        self.callback(name, rssi)

    async def start(self):

        scanner = BleakScanner(self.detection_callback)

        await scanner.start()

        while True:
            await asyncio.sleep(1)