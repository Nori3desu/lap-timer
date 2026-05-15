import time

from database import save_lap

# ゲートに入った判定
ENTER_RSSI_THRESHOLD = -45

# ゲートから出た判定
EXIT_RSSI_THRESHOLD = -55

# 最低ラップ時間
MIN_LAP_TIME_SEC = 5

# 通過後の無視時間
COOLDOWN_SEC = 3


class LapManager:
    def __init__(self):
        self.racers = {}

    def update(self, name, major, rssi):
        now = time.time()

        if name not in self.racers:
            self.racers[name] = {
                "inside": False,
                "last_lap_time": 0,
                "lap_count": 0,
                "last_seen": now,
            }

        racer = self.racers[name]
        racer["last_seen"] = now

        # まだゲート外の場合
        if not racer["inside"]:

            # 十分強いRSSIで初めてゲートIN
            if rssi >= ENTER_RSSI_THRESHOLD:

                # クールダウン中は無視
                if now - racer["last_lap_time"] < COOLDOWN_SEC:
                    return

                # 2周目以降、最低ラップ時間未満なら無視
                if (
                    racer["last_lap_time"] != 0
                    and now - racer["last_lap_time"] < MIN_LAP_TIME_SEC
                ):
                    return

                racer["lap_count"] += 1

                if racer["last_lap_time"] == 0:
                    lap_time = 0
                    print(f"{name} START major={major} rssi={rssi}")
                else:
                    lap_time = now - racer["last_lap_time"]
                    print(
                        f"{name} major={major} "
                        f"LAP {racer['lap_count']} "
                        f"{lap_time:.2f} sec rssi={rssi}"
                    )

                save_lap(
                    name,
                    major,
                    racer["lap_count"],
                    lap_time
                )

                racer["last_lap_time"] = now
                racer["inside"] = True

            return

        # すでにゲート内の場合
        # 十分弱くなったら、初めてゲート外へ戻す
        if rssi <= EXIT_RSSI_THRESHOLD:
            racer["inside"] = False
            print(f"{name} OUT rssi={rssi}")