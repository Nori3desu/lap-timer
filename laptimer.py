import asyncio
import time
from bleak import BleakScanner

# ===== 設定 =====

TARGET_NAME = "RaceCar01"

# 近距離判定RSSI
RSSI_THRESHOLD = -65

# 誤検知防止
COOLDOWN_SEC = 5

# ===== 状態 =====

last_lap_time = 0
lap_count = 0

# ===== コールバック =====

def detection_callback(device, advertisement_data):
    global last_lap_time
    global lap_count

    name = device.name
    rssi = advertisement_data.rssi

    if name != TARGET_NAME:
        return

    now = time.time()

    print(f"DETECT {name} RSSI={rssi}")

    # 近距離判定
    if rssi > RSSI_THRESHOLD:

        # クールダウン
        if now - last_lap_time > COOLDOWN_SEC:

            lap_count += 1

            if last_lap_time == 0:
                print(f"\n=== START ===")
            else:
                lap_time = now - last_lap_time
                print(f"\n=== LAP {lap_count} ===")
                print(f"LAP TIME: {lap_time:.2f} sec")

            last_lap_time = now

# ===== メイン =====

async def main():

    print("BLE Lap Timer Start")

    scanner = BleakScanner(detection_callback)

    await scanner.start()

    while True:
        await asyncio.sleep(1)

asyncio.run(main())

