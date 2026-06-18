import time

from database import save_lap, get_rssi_settings


class LapManager:
    def __init__(self):
        self.racers = {}

    def update(self, name, major, rssi):

        settings = get_rssi_settings()

        enter_rssi_threshold = settings["enter_rssi_threshold"]
        exit_rssi_threshold = settings["exit_rssi_threshold"]
        min_lap_time_sec = settings["min_lap_time_sec"]
        cooldown_sec = settings["cooldown_sec"]

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
            if rssi >= enter_rssi_threshold:

                # クールダウン中は無視
                if now - racer["last_lap_time"] < cooldown_sec:
                    return

                # 2周目以降、最低ラップ時間未満なら無視
                if (
                    racer["last_lap_time"] != 0
                    and now - racer["last_lap_time"] < min_lap_time_sec
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
        if rssi <= exit_rssi_threshold:
            racer["inside"] = False
            print(f"{name} OUT rssi={rssi}")