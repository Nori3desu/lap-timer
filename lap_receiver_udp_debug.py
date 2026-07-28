#!/usr/bin/env python3
"""
SGS Smart Gate System
UDP Lap Receiver Version 1.0

受信形式:
    RX-0001,TX-4DFE95,6B:EB:E2:4D:FE:95,-68

役割:
    1. ESP32-C3受信機からUDPを受信
    2. CSVを検証
    3. Pi5の受信時刻を採用
    4. RSSIによるENTRY / EXIT判定
    5. ラップをSQLiteとWeb用DBへ保存

現段階:
    RX-0001のみを判定に使用します。
    RX-0002の2台統合判定は次段階で追加します。
"""

from __future__ import annotations

import asyncio
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from database import save_lap as save_web_lap


# ============================================================
# 基本設定
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

# 詳細ログ用DB
DATABASE_PATH = BASE_DIR / "lap_timer.sqlite3"

# FastAPI / Web画面が使用しているDB
WEB_DATABASE_PATH = BASE_DIR / "lap_timer.db"

# Liteモードのリセット要求ファイル
RESET_REQUEST_PATH = BASE_DIR / "lite_reset.request"

UDP_BIND_ADDRESS = "0.0.0.0"
UDP_PORT = 5000
UDP_BUFFER_SIZE = 1024

# 現段階ではRX-0001のみでラップ判定
ACTIVE_RECEIVER_IDS = {"RX-0001", "RX-0002"}


# ============================================================
# ラップ判定設定
# ============================================================

ENTRY_RSSI_THRESHOLD = -55
EXIT_RSSI_THRESHOLD = -65
MINIMUM_LAP_SECONDS = 10.0

ENTRY_CONFIRM_SECONDS = 0.8
EXIT_CONFIRM_SECONDS = 1.5

# ENTRY判定デバッグ表示
DEBUG_ENTRY = True

DISPLAY_INTERVAL_SECONDS = 0.25
RSSI_LOG_INTERVAL_SECONDS = 1.0
STATUS_INTERVAL_SECONDS = 10.0

# UDPが途切れた場合、ENTRY/EXIT候補を破棄する時間
PACKET_TIMEOUT_SECONDS = 2.0


# ============================================================
# 登録送信機
# ============================================================

@dataclass(frozen=True)
class RegisteredTransmitter:
    serial_number: str
    udp_transmitter_id: str
    mac_address: str
    uuid: str
    major: int
    minor: int
    rider_name: str
    bike_name: str


REGISTERED_TRANSMITTERS = [
    RegisteredTransmitter(
        serial_number="TX-0001",
        udp_transmitter_id="TX-4DFE95",
        mac_address="6B:EB:E2:4D:FE:95",
        uuid="12345678-1234-1234-1234-123456789abc",
        major=1,
        minor=1,
        rider_name="河村",
        bike_name="YZ250FX",
    ),
]

TRANSMITTER_BY_UDP_ID = {
    transmitter.udp_transmitter_id.upper(): transmitter
    for transmitter in REGISTERED_TRANSMITTERS
}

TRANSMITTER_BY_MAC = {
    transmitter.mac_address.upper(): transmitter
    for transmitter in REGISTERED_TRANSMITTERS
}


# ============================================================
# 動作状態
# ============================================================

@dataclass
class TransmitterState:
    inside_detection_zone: bool = False
    last_lap_monotonic: float | None = None
    last_lap_datetime: datetime | None = None
    lap_count: int = 0

    last_display_monotonic: float = 0.0
    last_rssi_log_monotonic: float = 0.0
    last_packet_monotonic: float = 0.0

    last_rssi: int | None = None
    last_receiver_id: str | None = None

    waiting_for_clear_after_reset: bool = False
    entry_candidate_since: float | None = None
    exit_candidate_since: float | None = None


transmitter_states: dict[str, TransmitterState] = {
    transmitter.serial_number: TransmitterState()
    for transmitter in REGISTERED_TRANSMITTERS
}


@dataclass(frozen=True)
class UdpPacket:
    receiver_id: str
    udp_transmitter_id: str
    mac_address: str
    rssi: int
    received_at: datetime
    sender_ip: str
    sender_port: int


running = True

udp_packet_count = 0
valid_packet_count = 0
invalid_packet_count = 0
unknown_transmitter_count = 0
inactive_receiver_count = 0
last_status_monotonic = 0.0


# ============================================================
# SQLite
# ============================================================

def open_database() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=5.0,
    )

    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS transmitters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial_number TEXT NOT NULL UNIQUE,
            uuid TEXT NOT NULL,
            major INTEGER NOT NULL,
            minor INTEGER NOT NULL,
            rider_name TEXT NOT NULL,
            bike_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS laps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            serial_number TEXT NOT NULL,
            lap_number INTEGER NOT NULL,
            passed_at TEXT NOT NULL,
            lap_time_seconds REAL,
            rssi INTEGER NOT NULL,
            uuid TEXT NOT NULL,
            major INTEGER NOT NULL,
            minor INTEGER NOT NULL,
            device_address TEXT,
            device_name TEXT,
            FOREIGN KEY(serial_number)
                REFERENCES transmitters(serial_number)
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_laps_serial_number
        ON laps(serial_number, lap_number)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_laps_passed_at
        ON laps(passed_at)
        """
    )

    connection.commit()
    return connection


def register_transmitters(
    connection: sqlite3.Connection,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")

    for transmitter in REGISTERED_TRANSMITTERS:
        connection.execute(
            """
            INSERT INTO transmitters (
                serial_number,
                uuid,
                major,
                minor,
                rider_name,
                bike_name,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(serial_number) DO UPDATE SET
                uuid = excluded.uuid,
                major = excluded.major,
                minor = excluded.minor,
                rider_name = excluded.rider_name,
                bike_name = excluded.bike_name
            """,
            (
                transmitter.serial_number,
                transmitter.uuid.lower(),
                transmitter.major,
                transmitter.minor,
                transmitter.rider_name,
                transmitter.bike_name,
                now,
            ),
        )

    connection.commit()


def load_existing_lap_counts(
    connection: sqlite3.Connection,
) -> None:
    for transmitter in REGISTERED_TRANSMITTERS:
        row = connection.execute(
            """
            SELECT
                lap_number,
                passed_at
            FROM laps
            WHERE serial_number = ?
            ORDER BY lap_number DESC
            LIMIT 1
            """,
            (transmitter.serial_number,),
        ).fetchone()

        if row is None:
            continue

        lap_number, passed_at = row
        state = transmitter_states[transmitter.serial_number]
        state.lap_count = int(lap_number)

        try:
            state.last_lap_datetime = datetime.fromisoformat(passed_at)
        except (TypeError, ValueError):
            state.last_lap_datetime = None


def save_detail_lap(
    connection: sqlite3.Connection,
    transmitter: RegisteredTransmitter,
    state: TransmitterState,
    packet: UdpPacket,
    lap_time_seconds: float | None,
) -> None:
    connection.execute(
        """
        INSERT INTO laps (
            serial_number,
            lap_number,
            passed_at,
            lap_time_seconds,
            rssi,
            uuid,
            major,
            minor,
            device_address,
            device_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transmitter.serial_number,
            state.lap_count,
            packet.received_at.isoformat(timespec="milliseconds"),
            lap_time_seconds,
            packet.rssi,
            transmitter.uuid.lower(),
            transmitter.major,
            transmitter.minor,
            packet.mac_address,
            packet.receiver_id,
        ),
    )

    connection.commit()


# ============================================================
# Web管理画面用RSSIログ
# ============================================================

def save_web_rssi_log(
    transmitter: RegisteredTransmitter,
    packet: UdpPacket,
) -> None:
    connection: sqlite3.Connection | None = None

    try:
        connection = sqlite3.connect(
            WEB_DATABASE_PATH,
            timeout=5.0,
        )

        connection.execute(
            """
            INSERT INTO rssi_logs (
                mac_address,
                name,
                major,
                rssi,
                timestamp
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                packet.mac_address,
                transmitter.rider_name,
                transmitter.major,
                packet.rssi,
                packet.received_at.timestamp(),
            ),
        )

        connection.commit()

    except sqlite3.Error as error:
        print()
        print(
            f"[RSSIログ保存エラー] "
            f"{transmitter.serial_number}: {error}"
        )

    finally:
        if connection is not None:
            connection.close()


# ============================================================
# UDP解析
# ============================================================

def normalize_mac(value: str) -> str:
    return value.strip().upper().replace("-", ":")


def parse_udp_packet(
    raw_data: bytes,
    sender_address: tuple[str, int],
) -> UdpPacket | None:
    try:
        text = raw_data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None

    parts = [part.strip() for part in text.split(",")]

    if len(parts) != 4:
        return None

    receiver_id = parts[0].upper()
    udp_transmitter_id = parts[1].upper()
    mac_address = normalize_mac(parts[2])

    if not receiver_id.startswith("RX-"):
        return None

    if not udp_transmitter_id.startswith("TX-"):
        return None

    mac_parts = mac_address.split(":")

    if (
        len(mac_parts) != 6
        or any(len(part) != 2 for part in mac_parts)
    ):
        return None

    try:
        rssi = int(parts[3])
    except ValueError:
        return None

    if rssi < -127 or rssi > 20:
        return None

    sender_ip, sender_port = sender_address

    return UdpPacket(
        receiver_id=receiver_id,
        udp_transmitter_id=udp_transmitter_id,
        mac_address=mac_address,
        rssi=rssi,
        received_at=datetime.now(),
        sender_ip=sender_ip,
        sender_port=sender_port,
    )


def find_registered_transmitter(
    packet: UdpPacket,
) -> RegisteredTransmitter | None:
    by_udp_id = TRANSMITTER_BY_UDP_ID.get(
        packet.udp_transmitter_id
    )

    by_mac = TRANSMITTER_BY_MAC.get(
        packet.mac_address
    )

    if by_udp_id is not None and by_mac is not None:
        if by_udp_id.serial_number != by_mac.serial_number:
            print()
            print(
                "[UDP拒否] TX-IDとMACの登録先が一致しません: "
                f"{packet.udp_transmitter_id} / "
                f"{packet.mac_address}"
            )
            return None

    return by_udp_id or by_mac


# ============================================================
# ラップ判定
# ============================================================

def record_lap(
    connection: sqlite3.Connection,
    transmitter: RegisteredTransmitter,
    state: TransmitterState,
    packet: UdpPacket,
) -> None:
    now_monotonic = time.monotonic()

    if state.last_lap_monotonic is not None:
        elapsed = now_monotonic - state.last_lap_monotonic

        if elapsed < MINIMUM_LAP_SECONDS:
            remaining = MINIMUM_LAP_SECONDS - elapsed

            print()
            print(
                f"[判定保留] {transmitter.serial_number} "
                f"最短ラップ時間まで残り {remaining:.1f} 秒"
            )
            return

    lap_time_seconds: float | None = None

    if state.last_lap_monotonic is not None:
        lap_time_seconds = (
            now_monotonic - state.last_lap_monotonic
        )

    state.lap_count += 1
    state.last_lap_monotonic = now_monotonic
    state.last_lap_datetime = packet.received_at

    save_detail_lap(
        connection=connection,
        transmitter=transmitter,
        state=state,
        packet=packet,
        lap_time_seconds=lap_time_seconds,
    )

    try:
        save_web_lap(
            name=transmitter.rider_name,
            major=transmitter.major,
            lap_number=state.lap_count,
            lap_time=lap_time_seconds,
        )
    except Exception as error:
        print()
        print(
            f"[Webラップ保存エラー] "
            f"{transmitter.serial_number}: {error}"
        )

    print()
    print()
    print("============================================")
    print("          UDP ラップ検出")
    print("============================================")
    print(f"受信機     : {packet.receiver_id}")
    print(f"送信元IP   : {packet.sender_ip}:{packet.sender_port}")
    print(f"送信機     : {transmitter.serial_number}")
    print(f"UDP TX-ID  : {packet.udp_transmitter_id}")
    print(f"MAC        : {packet.mac_address}")
    print(f"ライダー   : {transmitter.rider_name}")
    print(f"車両       : {transmitter.bike_name}")
    print(f"ラップ番号 : {state.lap_count}")
    print(
        "通過時刻   : "
        f"{packet.received_at.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}"
    )

    if lap_time_seconds is None:
        print("ラップタイム: 初回通過")
    else:
        print(f"ラップタイム: {lap_time_seconds:.3f} 秒")

    print(f"RSSI       : {packet.rssi} dBm")
    print("============================================")
    print()


def process_packet(
    connection: sqlite3.Connection,
    transmitter: RegisteredTransmitter,
    packet: UdpPacket,
) -> None:
    state = transmitter_states[transmitter.serial_number]
    now_monotonic = time.monotonic()

    state.last_packet_monotonic = now_monotonic
    state.last_rssi = packet.rssi
    state.last_receiver_id = packet.receiver_id

    if state.waiting_for_clear_after_reset:
        if packet.rssi < EXIT_RSSI_THRESHOLD:
            state.waiting_for_clear_after_reset = False
            state.inside_detection_zone = False

            print()
            print(
                "[リセット後のゾーン離脱確認] "
                f"{transmitter.serial_number} "
                f"RSSI={packet.rssi} dBm"
            )
            print("次の通過から計測を開始します。")

        return

    if (
        now_monotonic - state.last_rssi_log_monotonic
        >= RSSI_LOG_INTERVAL_SECONDS
    ):
        save_web_rssi_log(
            transmitter=transmitter,
            packet=packet,
        )
        state.last_rssi_log_monotonic = now_monotonic

    if (
        now_monotonic - state.last_display_monotonic
        >= DISPLAY_INTERVAL_SECONDS
    ):
        zone_text = (
            "通過済み"
            if state.inside_detection_zone
            else "待機中"
        )

        print(
            "\r"
            f"{packet.receiver_id}  "
            f"{transmitter.serial_number}  "
            f"RSSI={packet.rssi:4d} dBm  "
            f"状態={zone_text:8s}  "
            f"ラップ={state.lap_count:3d}",
            end="",
            flush=True,
        )

        state.last_display_monotonic = now_monotonic

    if not state.inside_detection_zone:
        state.exit_candidate_since = None

        if packet.rssi >= ENTRY_RSSI_THRESHOLD:
            if state.entry_candidate_since is None:
                state.entry_candidate_since = now_monotonic

                if DEBUG_ENTRY:
                    print()
                    print(
                        f"[ENTRY開始] "
                        f"{packet.receiver_id} "
                        f"{transmitter.serial_number} "
                        f"RSSI={packet.rssi} dBm"
                    )

            entry_elapsed = (
                now_monotonic - state.entry_candidate_since
            )

            if DEBUG_ENTRY:
                print(
                    f"\r[ENTRY継続] "
                    f"{packet.receiver_id} "
                    f"{transmitter.serial_number} "
                    f"経過={entry_elapsed:.2f}秒 "
                    f"RSSI={packet.rssi} dBm",
                    end="",
                    flush=True,
                )

            if entry_elapsed >= ENTRY_CONFIRM_SECONDS:
                if DEBUG_ENTRY:
                    print()
                    print(
                        f"[ENTRY成立] "
                        f"{packet.receiver_id} "
                        f"{transmitter.serial_number} "
                        f"経過={entry_elapsed:.2f}秒 "
                        f"RSSI={packet.rssi} dBm"
                    )

                state.inside_detection_zone = True
                state.entry_candidate_since = None

                record_lap(
                    connection=connection,
                    transmitter=transmitter,
                    state=state,
                    packet=packet,
                )
        else:
            if (
                DEBUG_ENTRY
                and state.entry_candidate_since is not None
            ):
                entry_elapsed = (
                    now_monotonic - state.entry_candidate_since
                )

                print()
                print(
                    f"[ENTRYキャンセル] "
                    f"{packet.receiver_id} "
                    f"{transmitter.serial_number} "
                    f"経過={entry_elapsed:.2f}秒 "
                    f"RSSI={packet.rssi} dBm "
                    f"(しきい値={ENTRY_RSSI_THRESHOLD} dBm)"
                )

            state.entry_candidate_since = None

        return

    state.entry_candidate_since = None

    if packet.rssi < EXIT_RSSI_THRESHOLD:
        if state.exit_candidate_since is None:
            state.exit_candidate_since = now_monotonic

        exit_elapsed = (
            now_monotonic - state.exit_candidate_since
        )

        if exit_elapsed >= EXIT_CONFIRM_SECONDS:
            state.inside_detection_zone = False
            state.exit_candidate_since = None

            print()
            print(
                f"[ゾーン離脱] "
                f"{packet.receiver_id} "
                f"{transmitter.serial_number} "
                f"RSSI={packet.rssi} dBm"
            )
            print("次の通過を受け付けます。")
    else:
        state.exit_candidate_since = None


# ============================================================
# Liteモード練習リセット
# ============================================================

def process_lite_reset_request(
    connection: sqlite3.Connection,
) -> None:
    if not RESET_REQUEST_PATH.exists():
        return

    connection.execute("DELETE FROM laps")
    connection.commit()

    for state in transmitter_states.values():
        state.inside_detection_zone = True
        state.last_lap_monotonic = None
        state.last_lap_datetime = None
        state.lap_count = 0
        state.last_display_monotonic = 0.0
        state.last_rssi_log_monotonic = 0.0
        state.last_packet_monotonic = 0.0
        state.last_rssi = None
        state.last_receiver_id = None
        state.waiting_for_clear_after_reset = True
        state.entry_candidate_since = None
        state.exit_candidate_since = None

    try:
        RESET_REQUEST_PATH.unlink()
    except FileNotFoundError:
        pass

    print()
    print("============================================")
    print(" Liteモード練習データをリセットしました")
    print("============================================")


# ============================================================
# タイムアウト・ステータス
# ============================================================

def clear_stale_candidates() -> None:
    now_monotonic = time.monotonic()

    for state in transmitter_states.values():
        if state.last_packet_monotonic <= 0:
            continue

        if (
            now_monotonic - state.last_packet_monotonic
            < PACKET_TIMEOUT_SECONDS
        ):
            continue

        state.entry_candidate_since = None
        state.exit_candidate_since = None


def print_status() -> None:
    print()
    print()
    print("---------- SGS UDP RX STATUS ----------")
    print(f"Listen         : {UDP_BIND_ADDRESS}:{UDP_PORT}")
    print(
        "Active RX      : "
        + ", ".join(sorted(ACTIVE_RECEIVER_IDS))
    )
    print(f"UDP packets    : {udp_packet_count}")
    print(f"Valid packets  : {valid_packet_count}")
    print(f"Invalid packets: {invalid_packet_count}")
    print(f"Unknown TX     : {unknown_transmitter_count}")
    print(f"Inactive RX    : {inactive_receiver_count}")

    for transmitter in REGISTERED_TRANSMITTERS:
        state = transmitter_states[transmitter.serial_number]

        print(
            f"{transmitter.serial_number:<12}: "
            f"lap={state.lap_count:<3} "
            f"rssi={str(state.last_rssi):>4} "
            f"rx={state.last_receiver_id or '-'}"
        )

    print("---------------------------------------")
    print()


# ============================================================
# UDPプロトコル
# ============================================================

class SgsUdpProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        self.connection = connection
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(
        self,
        transport: asyncio.BaseTransport,
    ) -> None:
        self.transport = transport  # type: ignore[assignment]

        print()
        print("============================================")
        print(" SGS UDP Lap Receiver Version 1.0")
        print("============================================")
        print(
            f"[UDP] Listening on "
            f"{UDP_BIND_ADDRESS}:{UDP_PORT}"
        )
        print(
            "[UDP] Active receivers: "
            + ", ".join(sorted(ACTIVE_RECEIVER_IDS))
        )
        print("[UDP] Waiting for packets...")
        print()

    def datagram_received(
        self,
        data: bytes,
        address: tuple[str, int],
    ) -> None:
        global udp_packet_count
        global valid_packet_count
        global invalid_packet_count
        global unknown_transmitter_count
        global inactive_receiver_count

        udp_packet_count += 1

        packet = parse_udp_packet(data, address)

        if packet is None:
            invalid_packet_count += 1

            try:
                raw_text = data.decode(
                    "utf-8",
                    errors="replace",
                ).strip()
            except Exception:
                raw_text = repr(data)

            print()
            print(
                f"[UDP無効] {address[0]}:{address[1]} "
                f"{raw_text}"
            )
            return

        if packet.receiver_id not in ACTIVE_RECEIVER_IDS:
            inactive_receiver_count += 1
            return

        transmitter = find_registered_transmitter(packet)

        if transmitter is None:
            unknown_transmitter_count += 1

            print()
            print(
                "[未登録送信機] "
                f"RX={packet.receiver_id} "
                f"TX-ID={packet.udp_transmitter_id} "
                f"MAC={packet.mac_address} "
                f"RSSI={packet.rssi}"
            )
            return

        valid_packet_count += 1

        process_packet(
            connection=self.connection,
            transmitter=transmitter,
            packet=packet,
        )

    def error_received(
        self,
        exception: Exception,
    ) -> None:
        print()
        print(f"[UDPエラー] {exception}")

    def connection_lost(
        self,
        exception: Exception | None,
    ) -> None:
        if exception is None:
            print()
            print("[UDP] Listener stopped.")
        else:
            print()
            print(f"[UDP] Listener stopped: {exception}")


# ============================================================
# 起動・終了
# ============================================================

def request_shutdown() -> None:
    global running
    running = False


async def main() -> None:
    global last_status_monotonic

    connection = open_database()

    try:
        register_transmitters(connection)
        load_existing_lap_counts(connection)

        event_loop = asyncio.get_running_loop()

        for signal_name in (
            signal.SIGINT,
            signal.SIGTERM,
        ):
            try:
                event_loop.add_signal_handler(
                    signal_name,
                    request_shutdown,
                )
            except NotImplementedError:
                pass

        transport, _protocol = await event_loop.create_datagram_endpoint(
            lambda: SgsUdpProtocol(connection),
            local_addr=(UDP_BIND_ADDRESS, UDP_PORT),
        )

        last_status_monotonic = time.monotonic()

        try:
            while running:
                process_lite_reset_request(connection)
                clear_stale_candidates()

                now_monotonic = time.monotonic()

                if (
                    now_monotonic - last_status_monotonic
                    >= STATUS_INTERVAL_SECONDS
                ):
                    print_status()
                    last_status_monotonic = now_monotonic

                await asyncio.sleep(0.1)

        finally:
            transport.close()
            await asyncio.sleep(0)

    finally:
        connection.close()

    print()
    print("[SYSTEM] SGS UDP Lap Receiver ended.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
