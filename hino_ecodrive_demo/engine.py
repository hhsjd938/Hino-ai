"""Deterministic, timestamp-based EcoDrive analysis (no browser or Streamlit dependency)."""
from __future__ import annotations

import csv
import io
import json
import math
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


NAMES = dict(zip(map(str, range(1, 21)), [
    "行駛中", "怠速過久", "熄火", "引擎啟動", "地理圍籬", "急加速", "急減速", "超速",
    "DTC 錯誤碼", "引擎過熱", "引擎過載", "疲勞駕駛", "未繫安全帶", "使用通訊",
    "抽菸", "分心", "鏡頭遮蔽", "駕駛消失", "預警式防護系統", "車道偏離警示系統",
]))
NAMES.update(high_rpm="高 RPM", high_rpm_low_speed="高 RPM 低速")
MESSAGES = {
    "7": "請留意安全車距，避免不必要的急減速。",
    "8": "目前車速偏高，請留意道路速限，安全減速。",
    "6": "請平順加速，避免急踩油門。",
    "high_rpm_low_speed": "目前低速但轉速偏高，請避免不必要的高轉速操作。",
    "high_rpm": "目前轉速偏高，請平順操作並留意適當檔位。",
    "2": "車輛已長時間怠速，請視現場情況減少不必要怠速。",
}
PRIORITY = {code: i for i, code in enumerate(MESSAGES)}


@dataclass(frozen=True)
class Config:
    accel_one: float = 10
    accel_two: float = 5
    decel_two: float = -15
    rapid_cooldown: float = 10
    idle_seconds: float = 300
    idle_stop_speed: float = 0
    idle_reset_speed: float = 10
    idle_release_speed: float = 10
    idle_release_seconds: float = 30
    speed_margin: float = 10
    speed_absolute: float = 107
    speed_release: float = 95
    speed_hold: float = 5
    high_rpm: float = 2500
    low_speed: float = 30
    rpm_hold: float = 5
    max_gap: float = 10
    enabled: tuple[str, ...] = ("2", "6", "7", "8", "high_rpm", "high_rpm_low_speed")

    def validate(self) -> None:
        values = asdict(self)
        for name, value in values.items():
            if name != "enabled" and (not math.isfinite(value) or (value < 0 and name != "decel_two")):
                raise ValueError(f"無效參數：{name}")
        if self.decel_two >= 0:
            raise ValueError("急減速門檻必須小於 0")
        if any(getattr(self, name) <= 0 for name in (
            "idle_seconds", "idle_release_seconds", "speed_hold", "rpm_hold", "max_gap", "rapid_cooldown"
        )):
            raise ValueError("持續時間與間隔必須大於 0")
        if self.speed_release >= self.speed_absolute:
            raise ValueError("超速解除門檻必須小於觸發門檻")


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def read_replay(path: Path) -> list[dict]:
    return read_replay_bytes(path.read_bytes())


def read_replay_bytes(content: bytes) -> list[dict]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV 請儲存為 UTF-8 編碼（可含 BOM）") from error
    with io.StringIO(text, newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "speed", "rpm", "car_status", "can_status"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"CSV 缺少欄位：{', '.join(sorted(required.difference(reader.fieldnames or [])))}")
        rows = []
        for index, row in enumerate(reader, 2):
            try:
                timestamp = datetime.fromisoformat(row["timestamp"])
                reports = json.loads(row.get("event_reports") or "[]")
                if not isinstance(reports, list) or any(not isinstance(r, dict) for r in reports):
                    raise ValueError("事件欄位必須是物件陣列")
            except (ValueError, TypeError) as error:
                raise ValueError(f"CSV 第 {index} 列格式錯誤：{error}") from error
            rows.append({
                **row, "timestamp": timestamp, "event_reports": reports,
                **{key: number(row.get(key)) for key in (
                    "speed", "rpm", "car_status", "can_status", "mileage", "fuel", "speed_limit", "longitude", "latitude"
                )},
            })
    if len(rows) < 2:
        raise ValueError("至少需要兩筆可播放資料")
    ids = {(row.get("vehicle", ""), row.get("journey", "")) for row in rows}
    if len(ids) != 1:
        raise ValueError("一個 replay.csv 只能包含同一台車、同一趟行程")
    try:
        return sorted(rows, key=lambda row: row["timestamp"])
    except TypeError as error:
        raise ValueError("時間欄位不能混用有時區與無時區格式") from error


def valid(row: dict) -> bool:
    return (row.get("can_status") == 0 and row.get("car_status") in (0, 1, 2)
            and row.get("speed") is not None and row["speed"] >= 0
            and row.get("rpm") is not None and row["rpm"] >= 0)


def analyze(rows: list[dict], config: Config = Config(), *, real: bool = False) -> dict:
    """Only observations received up to each frame contribute to that frame's events."""
    config.validate()
    if len(rows) < 2:
        raise ValueError("至少需要兩筆資料")
    ordered = sorted(rows, key=lambda row: row["timestamp"])
    origin = ordered[0]["timestamp"]
    frames, events = [], []
    seen_reports: set[tuple] = set()
    active_official: set[str] = set()
    rapid_window: deque = deque(maxlen=4)
    last_rapid: dict[str, float] = {}
    holds: dict[str, float] = {}
    states: dict[str, bool] = {}
    idle_seconds = idle_release = 0.0
    idle_active = False
    speed_branches: dict[str, bool] = {"road": False, "absolute": False}

    def emit(code: str, at: float, row: dict, source: str, phase: str = "start", original: str | None = None) -> None:
        if code in MESSAGES and code not in config.enabled:
            return
        name = NAMES.get(code, f"事件 {code}")
        events.append({
            "id": len(events), "code": code, "name": name, "at": at,
            "time": row["timestamp"].isoformat(sep=" "), "original_time": original,
            "source": source, "phase": phase,
            "message": MESSAGES.get(code, "") if phase == "start" else "",
            "priority": PRIORITY.get(code, 99),
        })

    def condition(row: dict, key: str) -> bool:
        if not valid(row):
            return False
        speed, rpm = row["speed"], row["rpm"]
        if key == "high_rpm":
            return rpm > config.high_rpm
        if key == "high_rpm_low_speed":
            return rpm > config.high_rpm and speed < config.low_speed
        if key == "road":
            return row.get("speed_limit") is not None and row["speed_limit"] > 0 and speed >= row["speed_limit"] + config.speed_margin
        if key == "road_release":
            return row.get("speed_limit") is not None and row["speed_limit"] > 0 and speed <= row["speed_limit"]
        if key == "absolute":
            return speed >= config.speed_absolute
        if key == "absolute_release":
            return speed <= config.speed_release
        return False

    for index, row in enumerate(ordered):
        at = (row["timestamp"] - origin).total_seconds()
        prev = ordered[index - 1] if index else None
        dt = (row["timestamp"] - prev["timestamp"]).total_seconds() if prev else 0
        continuous = bool(prev and 0 < dt <= config.max_gap and valid(prev) and valid(row))
        gap = bool(prev and (dt <= 0 or dt > config.max_gap))
        acceleration = ((row["speed"] - prev["speed"]) / 3.6 / dt) if continuous else None

        if not continuous:
            # A gap closes estimates as unknown, not as a measured event release.
            holds.clear()
            states.clear()
            rapid_window.clear()
            idle_seconds = idle_release = 0.0
            idle_active = False
            speed_branches = {"road": False, "absolute": False}

        if real:
            for report in row.get("event_reports", []):
                code = str(report.get("type", ""))
                start = report.get("startTime")
                if not code or not start:
                    continue
                key = (row.get("vehicle"), code, str(start))
                if key not in seen_reports:
                    seen_reports.add(key)
                    emit(code, at, row, "車機回報", original=str(start))
                    if code in ("2", "8") and code in config.enabled:
                        active_official.add(code)
                # Only use the end time when it is actually present in a received report.
                end = report.get("endTime")
                end_key = (*key, "end")
                if end and end_key not in seen_reports:
                    try:
                        received_end = datetime.fromisoformat(str(end)) <= row["timestamp"]
                    except (ValueError, TypeError):
                        received_end = False
                    if received_end:
                        seen_reports.add(end_key)
                        emit(code, at, row, "車機回報", "end", str(end))
                        active_official.discard(code)

        # Idle gauge is an estimate even in real mode; only simulated mode emits inferred event 2.
        if continuous:
            if row["rpm"] == 0 or row["speed"] > config.idle_reset_speed:
                if not idle_active:
                    idle_seconds = 0.0
            elif (row["speed"] == config.idle_stop_speed and prev["speed"] == config.idle_stop_speed
                  and row["rpm"] != 0 and prev["rpm"] != 0):
                idle_seconds += dt
            if not real:
                if not idle_active and idle_seconds >= config.idle_seconds:
                    idle_active = True
                    emit("2", at, row, "模擬規則")
                if idle_active:
                    idle_release = idle_release + dt if (prev["speed"] >= config.idle_release_speed and row["speed"] >= config.idle_release_speed) else 0
                    if row["rpm"] == 0 or idle_release >= config.idle_release_seconds:
                        emit("2", at, row, "模擬規則", "end")
                        idle_active = False
                        idle_seconds = idle_release = 0.0

        # High RPM rules are custom in both modes, with observed endpoints required.
        for code in ("high_rpm_low_speed", "high_rpm"):
            ok = condition(row, code)
            holds[code] = holds.get(code, 0) + dt if continuous and ok and condition(prev, code) else 0
            if not ok and states.get(code):
                states[code] = False
                emit(code, at, row, "自訂規則", "end")
            if ok and holds[code] >= config.rpm_hold and not states.get(code):
                states[code] = True
                emit(code, at, row, "自訂規則")

        if not real:
            # No interpolation: four exactly consecutive one-second samples are mandatory.
            if prev and abs(dt - 1) > 1e-6:
                rapid_window.clear()
            if valid(row):
                rapid_window.append(row["speed"])
            if len(rapid_window) == 4:
                v1, v2, v3, v4 = rapid_window
                matches = {"6": v4 - v3 > config.accel_one and v4 - v2 > config.accel_two and v3 - v1 > config.accel_two,
                           "7": v4 - v2 <= config.decel_two and v3 - v1 <= config.decel_two}
                for code, matches_rule in matches.items():
                    if matches_rule and at - last_rapid.get(code, -math.inf) >= config.rapid_cooldown:
                        emit(code, at, row, "模擬規則")
                        last_rapid[code] = at

            was_speeding = any(speed_branches.values())
            for branch in speed_branches:
                holds[branch] = holds.get(branch, 0) + dt if continuous and condition(row, branch) and condition(prev, branch) else 0
                release = branch + "_release"
                holds[release] = holds.get(release, 0) + dt if continuous and condition(row, release) and condition(prev, release) else 0
                if not speed_branches[branch] and holds[branch] >= config.speed_hold:
                    speed_branches[branch] = True
                elif speed_branches[branch] and holds[release] >= config.speed_hold:
                    speed_branches[branch] = False
            is_speeding = any(speed_branches.values())
            if is_speeding and not was_speeding:
                emit("8", at, row, "模擬規則")
            elif was_speeding and not is_speeding:
                emit("8", at, row, "模擬規則", "end")

        active = set(active_official) if real else ({"2"} if idle_active else set()) | ({"8"} if any(speed_branches.values()) else set())
        active.update(code for code, state in states.items() if state)
        active.intersection_update(config.enabled)
        if "high_rpm_low_speed" in active:
            active.discard("high_rpm")
        def delta(field: str) -> float | None:
            values = [ordered[0].get(field), row.get(field)]
            return values[1] - values[0] if None not in values and values[1] >= values[0] else None
        frames.append({
            "at": at, "time": row["timestamp"].isoformat(sep=" "), "speed": row.get("speed"),
            "rpm": row.get("rpm"), "car_status": row.get("car_status"), "speed_limit": row.get("speed_limit"),
            "acceleration": acceleration, "idle_seconds": idle_seconds, "gap": gap,
            "signal_valid": valid(row), "distance_km": delta("mileage"), "fuel_l": delta("fuel"),
            "active": sorted(active, key=lambda code: PRIORITY.get(code, 99)),
        })

    counts = Counter(event["code"] for event in events if event["phase"] == "start")
    return {"frames": frames, "events": events, "counts": dict(counts), "config": asdict(config),
            "duration": frames[-1]["at"], "real": real, "names": NAMES, "messages": MESSAGES,
            "gaps": sum(frame["gap"] for frame in frames)}


SCENARIOS = {
    "normal": "正常駕駛", "high_rpm": "高 RPM", "high_rpm_low_speed": "高 RPM 低速",
    "acceleration": "急加速", "deceleration": "急減速", "idle": "長時間怠速", "speeding": "超速",
}


def scenario(name: str) -> list[dict]:
    if name not in SCENARIOS:
        raise ValueError("未知模擬情境")
    duration = 350 if name == "idle" else 55 if name == "speeding" else 30
    start = datetime(2026, 10, 11, 8)
    rows = []
    distance = 0.0
    for second in range(duration + 1):
        speed, rpm = 40.0, 1500.0
        if name == "high_rpm" and 5 <= second <= 18:
            speed, rpm = 50, 2800
        elif name == "high_rpm_low_speed":
            speed, rpm = 20, 2800 if 5 <= second <= 18 else 1500
        elif name == "acceleration":
            speed = [20, 24, 28, 45][second - 5] if 5 <= second <= 8 else (20 if second < 5 else 45)
        elif name == "deceleration":
            speed = [80, 70, 60, 50][second - 5] if 5 <= second <= 8 else (80 if second < 5 else 50)
        elif name == "idle":
            speed, rpm = (0, 750) if second <= 310 else (20, 1200)
        elif name == "speeding":
            speed = min(110, 40 + 5 * second) if second <= 25 else max(40, 110 - 5 * (second - 25))
        if second:
            distance += rows[-1]["speed"] / 3600
        rows.append({"timestamp": start + timedelta(seconds=second), "speed": speed, "rpm": rpm,
                     "car_status": 2 if speed == 0 else 1, "can_status": 0, "speed_limit": 80,
                     "vehicle": "SIMULATED", "journey": name, "mileage": 1000 + distance,
                     "fuel": None, "event_reports": []})
    return rows
