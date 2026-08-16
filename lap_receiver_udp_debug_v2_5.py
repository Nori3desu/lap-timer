#!/usr/bin/env python3
"""
SGS Smart Gate System
UDP Lap Receiver Version 2.5

受信形式:
    RX-0001,TX-4DFE95,6B:EB:E2:4D:FE:95,-68

役割:
    1. ESP32-C3受信機2台からUDPを受信
    2. RXごとに最新RSSIを個別保持
    3. 2台の受信結果をダイバーシティ統合してENTRY / EXITを判定
    4. どちらか一方のRXが十分強ければENTRY候補とする
    5. 両方のRXが弱くなったらEXITとする
    6. ラップをSQLiteとWeb用DBへ保存

前提:
    並走コースの誤検知対策は無効。単独コース用。
"""

from __future__ import annotations

import asyncio
import signal
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

from database import save_lap as save_web_lap


# ============================================================
# 基本設定
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "lap_timer.sqlite3"
WEB_DATABASE_PATH = BASE_DIR / "lap_timer.db"
RESET_REQUEST_PATH = BASE_DIR / "lite_reset.request"

UDP_BIND_ADDRESS = "0.0.0.0"
UDP_PORT = 5000
UDP_BUFFER_SIZE = 1024

ACTIVE_RECEIVER_IDS = {"RX-0001", "RX-0002"}


# ============================================================
# 2台ダイバーシティ・ラップ判定設定
# ============================================================

ENTRY_RSSI_THRESHOLD = -60
EXIT_RSSI_THRESHOLD = -68
MINIMUM_LAP_SECONDS = 10.0

# 上記はフォールバック用デフォルト値。
# 起動時に lap_timer.db の settings から読み込んで上書きする。

RECEIVER_DATA_TIMEOUT_SECONDS = 0.60

ENTRY_CONFIRM_SECONDS = 0.12
EXIT_CONFIRM_SECONDS = 1.0

DEBUG_ENTRY = True

DISPLAY_INTERVAL_SECONDS = 0.25

# Web用RSSIログ
RSSI_LOG_INTERVAL_SECONDS = 1.0

# 通過検証時の高密度ログ
RSSI_DENSE_LOG_INTERVAL_SECONDS = 0.20
RSSI_DENSE_POST_EXIT_SECONDS = 4.0

STATUS_INTERVAL_SECONDS = 10.0
RSSI_MOVING_AVERAGE_SAMPLES = 3


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
        serial_number="TX-0003",
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

@dataclass(frozen=True)
class UdpPacket:
    receiver_id: str
    udp_transmitter_id: str
    mac_address: str
    rssi: int
    received_at: datetime
    sender_ip: str
    sender_port: int


class GateState(Enum):
    WAIT = auto()
    ENTRY_CANDIDATE = auto()
    INSIDE = auto()
    EXIT_CANDIDATE = auto()


@dataclass
class ReceiverState:
    rssi: int | None = None
    rssi_samples: deque[int] = field(
        default_factory=lambda: deque(maxlen=RSSI_MOVING_AVERAGE_SAMPLES)
    )
    last_packet_monotonic: float = 0.0
    last_packet: UdpPacket | None = None

    @property
    def averaged_rssi(self) -> int | None:
        if not self.rssi_samples:
            return None
        return round(sum(self.rssi_samples) / len(self.rssi_samples))


@dataclass
class TransmitterState:
    gate_state: GateState = GateState.WAIT
    last_lap_monotonic: float | None = None
    last_lap_datetime: datetime | None = None
    lap_count: int = 0

    receiver_states: dict[str, ReceiverState] = field(
        default_factory=lambda: {
            receiver_id: ReceiverState()
            for receiver_id in ACTIVE_RECEIVER_IDS
        }
    )

    last_display_monotonic: float = 0.0

    last_rssi_log_monotonic: dict[str, float] = field(
        default_factory=lambda: {
            receiver_id: 0.0
            for receiver_id in ACTIVE_RECEIVER_IDS
        }
    )

    # EXIT成立後も一定時間、高密度RSSIログを継続する
    dense_log_until_monotonic: float = 0.0

    waiting_for_clear_after_reset: bool = False
    entry_candidate_since: float | None = None
    exit_candidate_since: float | None = None

    last_gate_valid: bool = False
    last_combined_rssi: int | None = None
    last_rssi_difference: int | None = None


transmitter_states: dict[str, TransmitterState] = {
    transmitter.serial_number: TransmitterState()
    for transmitter in REGISTERED_TRANSMITTERS
}


running = True

udp_packet_count = 0
valid_packet_count = 0
invalid_packet_count = 0
unknown_transmitter_count = 0
inactive_receiver_count = 0
last_status_monotonic = 0.0


# ============================================================
# Web設定読み込み
# ============================================================

def load_web_rssi_settings() -> None:
    global ENTRY_RSSI_THRESHOLD
    global EXIT_RSSI_THRESHOLD
    global MINIMUM_LAP_SECONDS

    connection = None

    try:
        connection = sqlite3.connect(
            WEB_DATABASE_PATH,
            timeout=5.0,
        )

        rows = connection.execute(
            """
            SELECT key, value
            FROM settings
            WHERE key IN (
                'enter_rssi_threshold',
                'exit_rssi_threshold',
                'min_lap_time_sec'
            )
            """
        ).fetchall()

        settings = {
            key: value
            for key, value in rows
        }

        ENTRY_RSSI_THRESHOLD = int(
            settings.get(
                "enter_rssi_threshold",
                ENTRY_RSSI_THRESHOLD,
            )
        )

        EXIT_RSSI_THRESHOLD = int(
            settings.get(
                "exit_rssi_threshold",
                EXIT_RSSI_THRESHOLD,
            )
        )

        MINIMUM_LAP_SECONDS = float(
            settings.get(
                "min_lap_time_sec",
                MINIMUM_LAP_SECONDS,
            )
        )

        print()
        print("============================================")
        print(" Web RSSI設定を読み込みました")
        print(f" ENTER   : {ENTRY_RSSI_THRESHOLD} dBm")
        print(f" EXIT    : {EXIT_RSSI_THRESHOLD} dBm")
        print(f" MIN LAP : {MINIMUM_LAP_SECONDS:.1f} 秒")
        print("============================================")
        print()

    except (sqlite3.Error, ValueError, TypeError) as error:
        print()
        print(
            "[Web RSSI設定読込エラー] "
            f"デフォルト値を使用します: {error}"
        )

    finally:
        if connection is not None:
            connection.close()


# ============================================================
# SQLite
# ============================================================

def open_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=5.0)
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
        CREATE INDEX IF NOT EXISTS idx_laps_serial_number
        ON laps(serial_number, lap_number)
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_laps_passed_at
        ON laps(passed_at)
        """
    )

    connection.commit()
    return connection


def register_transmitters(connection: sqlite3.Connection) -> None:
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


def load_existing_lap_counts(connection: sqlite3.Connection) -> None:
    for transmitter in REGISTERED_TRANSMITTERS:
        row = connection.execute(
            """
            SELECT lap_number, passed_at
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
    combined_rssi: int,
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
            combined_rssi,
            transmitter.uuid.lower(),
            transmitter.major,
            transmitter.minor,
            packet.mac_address,
            "RX-0001+RX-0002",
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
        connection = sqlite3.connect(WEB_DATABASE_PATH, timeout=5.0)
        connection.execute(
            """
            INSERT INTO rssi_logs (
                mac_address,
                receiver_id,
                name,
                major,
                rssi,
                timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                packet.mac_address,
                packet.receiver_id,
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
    if len(mac_parts) != 6 or any(len(part) != 2 for part in mac_parts):
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
    by_udp_id = TRANSMITTER_BY_UDP_ID.get(packet.udp_transmitter_id)
    by_mac = TRANSMITTER_BY_MAC.get(packet.mac_address)

    if by_udp_id is not None and by_mac is not None:
        if by_udp_id.serial_number != by_mac.serial_number:
            print()
            print(
                "[UDP拒否] TX-IDとMACの登録先が一致しません: "
                f"{packet.udp_transmitter_id} / {packet.mac_address}"
            )
            return None

    return by_udp_id or by_mac


# ============================================================
# 2台統合判定
# ============================================================

def get_fresh_receiver_data(
    state: TransmitterState,
    now_monotonic: float,
) -> dict[str, ReceiverState]:
    fresh: dict[str, ReceiverState] = {}

    for receiver_id in ACTIVE_RECEIVER_IDS:
        receiver_state = state.receiver_states[receiver_id]

        if receiver_state.rssi is None:
            continue

        age = now_monotonic - receiver_state.last_packet_monotonic

        if age <= RECEIVER_DATA_TIMEOUT_SECONDS:
            fresh[receiver_id] = receiver_state

    return fresh


def select_reference_packet(
    fresh: dict[str, ReceiverState],
) -> UdpPacket | None:
    valid_states = [
        receiver_state
        for receiver_state in fresh.values()
        if receiver_state.rssi is not None
        and receiver_state.last_packet is not None
    ]

    if not valid_states:
        return None

    weakest_state = min(
        valid_states,
        key=lambda receiver_state: receiver_state.rssi,
    )
    return weakest_state.last_packet


def record_lap(
    connection: sqlite3.Connection,
    transmitter: RegisteredTransmitter,
    state: TransmitterState,
    packet: UdpPacket,
    combined_rssi: int,
    rssi_difference: int,
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
        lap_time_seconds = now_monotonic - state.last_lap_monotonic

    state.lap_count += 1
    state.last_lap_monotonic = now_monotonic
    state.last_lap_datetime = packet.received_at

    save_detail_lap(
        connection=connection,
        transmitter=transmitter,
        state=state,
        packet=packet,
        lap_time_seconds=lap_time_seconds,
        combined_rssi=combined_rssi,
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

    rx1_rssi = state.receiver_states["RX-0001"].rssi
    rx2_rssi = state.receiver_states["RX-0002"].rssi

    print()
    print()
    print("============================================")
    print("       UDP 2台統合ラップ検出")
    print("============================================")
    print("受信機     : RX-0001 + RX-0002")
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

    print(f"RX-0001    : {rx1_rssi} dBm")
    print(f"RX-0002    : {rx2_rssi} dBm")
    print(f"統合RSSI   : {combined_rssi} dBm")
    print(f"RSSI差     : {rssi_difference} dB")
    print("============================================")
    print()


def update_receiver_state(
    transmitter: RegisteredTransmitter,
    packet: UdpPacket,
) -> None:
    state = transmitter_states[transmitter.serial_number]
    now_monotonic = time.monotonic()

    receiver_state = state.receiver_states[packet.receiver_id]
    receiver_state.rssi = packet.rssi
    receiver_state.rssi_samples.append(packet.rssi)
    receiver_state.last_packet_monotonic = now_monotonic
    receiver_state.last_packet = packet

    last_log_monotonic = (
        state.last_rssi_log_monotonic.get(
            packet.receiver_id,
            0.0,
        )
    )

    # --------------------------------------------------------
    # 通過中だけRSSIログを高密度化する
    #
    # ・現在GateStateがWAIT以外
    # ・今回のRSSIがENTER以上
    # ・EXIT成立後4秒以内
    #
    # のどれかなら0.2秒間隔。
    # 通常時は従来どおり1.0秒間隔。
    # --------------------------------------------------------

    # どちらか一方のRXがENTER以上を検出した時点で、
    # この送信機全体を高密度ログモードにする。
    #
    # これによりRX1/RX2の保存密度を揃え、
    # 通過検証時に両RXのRSSI推移を比較しやすくする。
    if packet.rssi >= ENTRY_RSSI_THRESHOLD:
        state.dense_log_until_monotonic = max(
            state.dense_log_until_monotonic,
            now_monotonic + RSSI_DENSE_POST_EXIT_SECONDS,
        )

    dense_logging = (
        state.gate_state != GateState.WAIT
        or now_monotonic < state.dense_log_until_monotonic
    )

    log_interval = (
        RSSI_DENSE_LOG_INTERVAL_SECONDS
        if dense_logging
        else RSSI_LOG_INTERVAL_SECONDS
    )

    if (
        now_monotonic - last_log_monotonic
        >= log_interval
    ):
        save_web_rssi_log(
            transmitter=transmitter,
            packet=packet,
        )

        state.last_rssi_log_monotonic[
            packet.receiver_id
        ] = now_monotonic


def evaluate_gate(
    connection: sqlite3.Connection,
    transmitter: RegisteredTransmitter,
) -> None:

    state = transmitter_states[transmitter.serial_number]
    now_monotonic = time.monotonic()

    fresh = get_fresh_receiver_data(state, now_monotonic)
    both_receivers_fresh = len(fresh) == len(ACTIVE_RECEIVER_IDS)

    rx1 = fresh.get("RX-0001")
    rx2 = fresh.get("RX-0002")

    rx1_avg = rx1.averaged_rssi if rx1 is not None else None
    rx2_avg = rx2.averaged_rssi if rx2 is not None else None

    # 単独コース用ダイバーシティ判定
    # RX1/RX2のどちらか一方が強ければ通過候補とする。
    # コース端の走行やライダー身体による遮蔽に強くする。
    fresh_values = [
        receiver_state.averaged_rssi
        for receiver_state in fresh.values()
        if receiver_state.averaged_rssi is not None
    ]

    combined_rssi: int | None = max(fresh_values) if fresh_values else None
    rssi_difference: int | None = None
    if both_receivers_fresh and rx1_avg is not None and rx2_avg is not None:
        rssi_difference = abs(rx1_avg - rx2_avg)

    gate_valid = bool(fresh_values)
    entry_signal = (
        combined_rssi is not None
        and combined_rssi >= ENTRY_RSSI_THRESHOLD
    )

    state.last_gate_valid = gate_valid
    state.last_combined_rssi = combined_rssi
    state.last_rssi_difference = rssi_difference

    if now_monotonic - state.last_display_monotonic >= DISPLAY_INTERVAL_SECONDS:
        rx1_text = str(rx1_avg) if rx1_avg is not None else "-"
        rx2_text = str(rx2_avg) if rx2_avg is not None else "-"
        if entry_signal:
            judge_text = "成立候補"
        else:
            judge_text = "待機"
        print(
            "\r"
            f"{transmitter.serial_number}  "
            f"RX1平均={rx1_text:>4}  "
            f"RX2平均={rx2_text:>4}  "
            f"差={str(rssi_difference):>3}  "
            f"判定={judge_text:8s}  "
            f"状態={state.gate_state.name:15s}  "
            f"ラップ={state.lap_count:3d}",
            end="",
            flush=True,
        )
        state.last_display_monotonic = now_monotonic

    rx1_state = state.receiver_states["RX-0001"]
    rx2_state = state.receiver_states["RX-0002"]

    rx1_has_data = (
        rx1_state.rssi is not None
        and rx1_state.last_packet_monotonic > 0.0
    )

    rx2_has_data = (
        rx2_state.rssi is not None
        and rx2_state.last_packet_monotonic > 0.0
    )

    rx1_age = (
        now_monotonic - rx1_state.last_packet_monotonic
        if rx1_has_data
        else None
    )

    rx2_age = (
        now_monotonic - rx2_state.last_packet_monotonic
        if rx2_has_data
        else None
    )

    rx1_timed_out = (
        rx1_has_data
        and rx1_age is not None
        and rx1_age > RECEIVER_DATA_TIMEOUT_SECONDS
    )

    rx2_timed_out = (
        rx2_has_data
        and rx2_age is not None
        and rx2_age > RECEIVER_DATA_TIMEOUT_SECONDS
    )

    rx1_below_exit = (
        rx1_avg is not None
        and rx1_avg < EXIT_RSSI_THRESHOLD
    )

    rx2_below_exit = (
        rx2_avg is not None
        and rx2_avg < EXIT_RSSI_THRESHOLD
    )

    rx1_exit_ready = (
        rx1_has_data
        and (
            rx1_below_exit
            or rx1_timed_out
        )
    )

    rx2_exit_ready = (
        rx2_has_data
        and (
            rx2_below_exit
            or rx2_timed_out
        )
    )

    both_below_exit = (
        rx1_exit_ready
        and rx2_exit_ready
    )

    if state.waiting_for_clear_after_reset:
        if both_below_exit:
            if state.exit_candidate_since is None:
                state.exit_candidate_since = now_monotonic
            if now_monotonic - state.exit_candidate_since >= EXIT_CONFIRM_SECONDS:
                state.waiting_for_clear_after_reset = False
                state.gate_state = GateState.WAIT
                state.exit_candidate_since = None
                print()
                print(f"[リセット後のゾーン離脱確認] {transmitter.serial_number}")
                print("次の通過から計測を開始します。")
        else:
            state.exit_candidate_since = None
        return

    if state.gate_state == GateState.WAIT:
        if entry_signal:
            state.gate_state = GateState.ENTRY_CANDIDATE
            state.entry_candidate_since = now_monotonic
            if DEBUG_ENTRY:
                print()
                print(
                    f"[ダイバーシティENTRY開始] {transmitter.serial_number} "
                    f"RX1平均={rx1_avg} RX2平均={rx2_avg} "
                    f"差={rssi_difference} dB"
                )
        return

    if state.gate_state == GateState.ENTRY_CANDIDATE:
        if not entry_signal:
            elapsed = (
                now_monotonic - state.entry_candidate_since
                if state.entry_candidate_since is not None else 0.0
            )
            if DEBUG_ENTRY:
                print()
                print(f"[ダイバーシティENTRYキャンセル] {transmitter.serial_number} 経過={elapsed:.2f}秒")
            state.entry_candidate_since = None
            state.gate_state = GateState.WAIT
            return

        if state.entry_candidate_since is None:
            state.entry_candidate_since = now_monotonic

        entry_elapsed = now_monotonic - state.entry_candidate_since
        if entry_elapsed < ENTRY_CONFIRM_SECONDS:
            return

        reference_packet = select_reference_packet(fresh)
        if reference_packet is None or combined_rssi is None or rssi_difference is None:
            return

        if DEBUG_ENTRY:
            print()
            print(
                f"[ダイバーシティENTRY成立] {transmitter.serial_number} "
                f"経過={entry_elapsed:.2f}秒 統合RSSI={combined_rssi} dBm "
                f"差={rssi_difference} dB"
            )

        state.entry_candidate_since = None
        state.gate_state = GateState.INSIDE
        record_lap(
            connection=connection,
            transmitter=transmitter,
            state=state,
            packet=reference_packet,
            combined_rssi=combined_rssi,
            rssi_difference=rssi_difference,
        )
        return

    if state.gate_state == GateState.INSIDE:
        if both_below_exit:
            state.gate_state = GateState.EXIT_CANDIDATE
            state.exit_candidate_since = now_monotonic
            print()
            print(f"[ダイバーシティEXIT候補] {transmitter.serial_number}")
        return

    if state.gate_state == GateState.EXIT_CANDIDATE:
        if not both_below_exit:
            state.exit_candidate_since = None
            state.gate_state = GateState.INSIDE
            print()
            print(f"[ダイバーシティEXITキャンセル] {transmitter.serial_number}")
            return

        if state.exit_candidate_since is None:
            state.exit_candidate_since = now_monotonic

        if now_monotonic - state.exit_candidate_since >= EXIT_CONFIRM_SECONDS:
            state.exit_candidate_since = None

            state.dense_log_until_monotonic = (
                now_monotonic
                + RSSI_DENSE_POST_EXIT_SECONDS
            )

            state.gate_state = GateState.WAIT
            print()
            print(f"[ダイバーシティゾーン離脱] {transmitter.serial_number}")
            print("次の通過を受け付けます。")

def evaluate_all_gates(connection: sqlite3.Connection) -> None:
    for transmitter in REGISTERED_TRANSMITTERS:
        evaluate_gate(connection, transmitter)


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
        state.gate_state = GateState.INSIDE
        state.last_lap_monotonic = None
        state.last_lap_datetime = None
        state.lap_count = 0

        state.receiver_states = {
            receiver_id: ReceiverState()
            for receiver_id in ACTIVE_RECEIVER_IDS
        }

        state.last_display_monotonic = 0.0

        state.last_rssi_log_monotonic = {
            receiver_id: 0.0
            for receiver_id in ACTIVE_RECEIVER_IDS
        }

        state.dense_log_until_monotonic = 0.0

        state.waiting_for_clear_after_reset = True
        state.entry_candidate_since = None
        state.exit_candidate_since = None
        state.last_gate_valid = False
        state.last_combined_rssi = None
        state.last_rssi_difference = None

    try:
        RESET_REQUEST_PATH.unlink()
    except FileNotFoundError:
        pass

    print()
    print("============================================")
    print(" Liteモード練習データをリセットしました")
    print("============================================")


# ============================================================
# ステータス
# ============================================================

def print_status() -> None:
    print()
    print()
    print("---------- SGS UDP RX STATUS ----------")
    print("Version        : 2.5")
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
        rx1_rssi = state.receiver_states["RX-0001"].averaged_rssi
        rx2_rssi = state.receiver_states["RX-0002"].averaged_rssi

        print(
            f"{transmitter.serial_number:<12}: "
            f"lap={state.lap_count:<3} "
            f"rx1={str(rx1_rssi):>4} "
            f"rx2={str(rx2_rssi):>4} "
            f"diff={str(state.last_rssi_difference):>4} "
            f"gate={'OK' if state.last_gate_valid else 'NG'} "
            f"state={state.gate_state.name}"
        )

    print("---------------------------------------")
    print()


# ============================================================
# Battery最新状態DB
# ============================================================

def initialize_battery_database() -> None:
    """
    Web側DB(lap_timer.db)にBattery最新状態テーブルを作成する。
    既存のlaps/devices/rssi_logs等は変更しない。
    """
    connection = sqlite3.connect(WEB_DATABASE_PATH, timeout=5.0)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS transmitter_battery (
                serial_number TEXT PRIMARY KEY,
                udp_transmitter_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                battery_percent INTEGER NOT NULL,
                voltage_mv INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS transmitter_battery_receivers (
                serial_number TEXT NOT NULL,
                udp_transmitter_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                battery_percent INTEGER NOT NULL,
                voltage_mv INTEGER NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (serial_number, receiver_id)
            )
            """
        )

        connection.commit()
    finally:
        connection.close()


def find_registered_transmitter_by_udp_id(
    udp_transmitter_id: str,
) -> RegisteredTransmitter | None:
    return TRANSMITTER_BY_UDP_ID.get(
        udp_transmitter_id.upper()
    )


def save_battery_status(
    transmitter: RegisteredTransmitter,
    receiver_id: str,
    battery_percent: int,
    battery_millivolts: int,
) -> None:
    """
    送信機ごとに最新Battery状態1件だけをUPSERT保存する。
    """
    connection = sqlite3.connect(WEB_DATABASE_PATH, timeout=5.0)
    try:
        now_timestamp = time.time()

        # 互換用：TXごとの最新1件
        connection.execute(
            """
            INSERT INTO transmitter_battery (
                serial_number,
                udp_transmitter_id,
                receiver_id,
                battery_percent,
                voltage_mv,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(serial_number) DO UPDATE SET
                udp_transmitter_id = excluded.udp_transmitter_id,
                receiver_id = excluded.receiver_id,
                battery_percent = excluded.battery_percent,
                voltage_mv = excluded.voltage_mv,
                updated_at = excluded.updated_at
            """,
            (
                transmitter.serial_number,
                transmitter.udp_transmitter_id,
                receiver_id,
                battery_percent,
                battery_millivolts,
                now_timestamp,
            ),
        )

        # RX別：TX × RX ごとの最新1件
        connection.execute(
            """
            INSERT INTO transmitter_battery_receivers (
                serial_number,
                udp_transmitter_id,
                receiver_id,
                battery_percent,
                voltage_mv,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(serial_number, receiver_id) DO UPDATE SET
                udp_transmitter_id = excluded.udp_transmitter_id,
                battery_percent = excluded.battery_percent,
                voltage_mv = excluded.voltage_mv,
                updated_at = excluded.updated_at
            """,
            (
                transmitter.serial_number,
                transmitter.udp_transmitter_id,
                receiver_id,
                battery_percent,
                battery_millivolts,
                now_timestamp,
            ),
        )

        connection.commit()
    finally:
        connection.close()


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
        print(" SGS UDP Lap Receiver Version 2.5")
        print("============================================")
        print(
            f"[UDP] Listening on "
            f"{UDP_BIND_ADDRESS}:{UDP_PORT}"
        )
        print(
            "[UDP] Active receivers: "
            + ", ".join(sorted(ACTIVE_RECEIVER_IDS))
        )
        print("[GATE] Diversity mode: RX-0001 OR RX-0002")
        print("[GATE] Parallel-course rejection: disabled")
        print(f"[GATE] RSSI moving average: {RSSI_MOVING_AVERAGE_SAMPLES} samples")
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

        # ============================================================
        # Battery UDP
        #
        # 形式:
        # BAT,RX-0001,TX-4DFE95,73,3987
        #
        # Battery UDPは通常のラップUDPとは完全に分離して処理する。
        # 通常の4項目UDP解析(parse_udp_packet)には渡さない。
        # ============================================================

        try:
            raw_text = data.decode(
                "utf-8",
                errors="strict",
            ).strip()
        except UnicodeDecodeError:
            raw_text = ""

        if raw_text.startswith("BAT,"):
            parts = [
                part.strip()
                for part in raw_text.split(",")
            ]

            if len(parts) != 5:
                print()
                print(
                    f"[BAT-UDP無効] "
                    f"{address[0]}:{address[1]} "
                    f"{raw_text}"
                )
                return

            receiver_id = parts[1].upper()
            udp_transmitter_id = parts[2].upper()

            try:
                battery_percent = int(parts[3])
                battery_millivolts = int(parts[4])
            except ValueError:
                print()
                print(
                    f"[BAT-UDP無効] "
                    f"数値変換エラー: {raw_text}"
                )
                return

            if receiver_id not in ACTIVE_RECEIVER_IDS:
                print()
                print(
                    f"[BAT-UDP無効] "
                    f"未使用RX: {receiver_id}"
                )
                return

            if not udp_transmitter_id.startswith("TX-"):
                print()
                print(
                    f"[BAT-UDP無効] "
                    f"TX-ID不正: {udp_transmitter_id}"
                )
                return

            if not 0 <= battery_percent <= 100:
                print()
                print(
                    f"[BAT-UDP無効] "
                    f"Battery%不正: {battery_percent}"
                )
                return

            if not 2500 <= battery_millivolts <= 5000:
                print()
                print(
                    f"[BAT-UDP無効] "
                    f"電圧不正: {battery_millivolts}mV"
                )
                return

            transmitter = find_registered_transmitter_by_udp_id(
                udp_transmitter_id
            )

            if transmitter is None:
                print()
                print(
                    f"[BAT-UDP無効] "
                    f"未登録TX: {udp_transmitter_id}"
                )
                return

            battery_voltage = battery_millivolts / 1000.0

            try:
                save_battery_status(
                    transmitter=transmitter,
                    receiver_id=receiver_id,
                    battery_percent=battery_percent,
                    battery_millivolts=battery_millivolts,
                )
            except sqlite3.Error as error:
                print()
                print(
                    f"[BAT-DB保存エラー] "
                    f"{transmitter.serial_number}: {error}"
                )
                return

            print()
            print(
                "[BAT-UDP] "
                f"RX={receiver_id} "
                f"TX={udp_transmitter_id} "
                f"Serial={transmitter.serial_number} "
                f"Battery={battery_percent}% "
                f"Voltage={battery_voltage:.3f}V "
                f"DB=OK"
            )

            # Battery UDPはラップ判定へ渡さない
            return

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

        update_receiver_state(
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

    load_web_rssi_settings()

    connection = open_database()

    try:
        register_transmitters(connection)
        load_existing_lap_counts(connection)
        initialize_battery_database()

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
                evaluate_all_gates(connection)

                now_monotonic = time.monotonic()

                if (
                    now_monotonic - last_status_monotonic
                    >= STATUS_INTERVAL_SECONDS
                ):
                    print_status()
                    last_status_monotonic = now_monotonic

                await asyncio.sleep(0.05)

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
