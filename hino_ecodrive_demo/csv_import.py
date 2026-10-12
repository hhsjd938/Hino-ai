"""Validate an uploaded trip in memory without replacing the bundled sample."""
from __future__ import annotations

import hashlib

from engine import Config, analyze, read_replay_bytes, valid

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def uploaded_data(content: bytes, filename: str, config: Config):
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("請匯出單趟行程；CSV 大小需在 20 MB 以內")
    rows = read_replay_bytes(content)
    return analyze_uploaded(rows, filename, config, hashlib.sha256(content).hexdigest())


def analyze_uploaded(rows: list[dict], filename: str, config: Config, content_sha256: str):
    if not any(valid(row) for row in rows):
        raise ValueError("沒有有效車況：請檢查車速、RPM、car_status（0/1/2）與 can_status（正常為 0）")
    metadata = {
        "vehicle": rows[0].get("vehicle") or "未提供車輛",
        "journey": rows[0].get("journey") or "匯入行程",
        "reference": None,
        "upload_filename": filename,
        "replay_sha256": content_sha256,
        "records": len(rows),
    }
    return analyze(rows, config, real=True), metadata
