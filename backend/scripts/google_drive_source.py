"""Read the current HINO workbook directly from Google Drive.

The downloaded XLSX is a temporary working copy and is removed automatically
when the process exits. It is never treated as a user-uploaded source file.
"""

from __future__ import annotations

import atexit
import os
import re
import tempfile
from pathlib import Path

import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


DEFAULT_DRIVE_FILE = "1F68Xl4TACl7iGvES-0g0HeqDGxoBT639"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def add_drive_argument(parser) -> None:
    parser.add_argument(
        "--drive-file",
        default=os.getenv("HINO_DRIVE_FILE", DEFAULT_DRIVE_FILE),
        help="Google Drive file ID or sharing URL (default: HINO_DRIVE_FILE).",
    )


def _file_id(reference: str) -> str:
    value = reference.strip()
    for pattern in (r"/d/([A-Za-z0-9_-]+)", r"[?&]id=([A-Za-z0-9_-]+)"):
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    raise ValueError("--drive-file must be a Google Drive file ID or sharing URL")


class DriveWorkbook:
    def __init__(self, reference: str):
        self.file_id = _file_id(reference)
        credentials, _ = google.auth.default(scopes=[DRIVE_READONLY_SCOPE])
        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._metadata: dict | None = None

    def metadata(self) -> dict:
        if self._metadata is None:
            self._metadata = (
                self.service.files()
                .get(
                    fileId=self.file_id,
                    supportsAllDrives=True,
                    fields="id,name,mimeType,size,modifiedTime,md5Checksum,version,capabilities(canDownload)",
                )
                .execute()
            )
            if self._metadata.get("mimeType") != XLSX_MIME_TYPE:
                raise RuntimeError(
                    f"Drive file is {self._metadata.get('mimeType')!r}; expected an XLSX workbook"
                )
            if self._metadata.get("capabilities", {}).get("canDownload") is False:
                raise PermissionError("The signed-in Google account cannot download this Drive file")
        return self._metadata

    def identity(self) -> dict:
        metadata = self.metadata()
        return {
            "provider": "google_drive",
            "file_id": metadata["id"],
            "name": metadata["name"],
            "size": int(metadata["size"]) if metadata.get("size") else None,
            "modified_time": metadata.get("modifiedTime"),
            "version": metadata.get("version"),
            "md5_checksum": metadata.get("md5Checksum"),
        }

    def download(self) -> Path:
        metadata = self.metadata()
        temporary = tempfile.NamedTemporaryFile(prefix="hino_drive_", suffix=".xlsx", delete=False)
        path = Path(temporary.name)
        try:
            request = self.service.files().get_media(fileId=self.file_id, supportsAllDrives=True)
            downloader = MediaIoBaseDownload(temporary, request, chunksize=8 * 1024 * 1024)
            done = False
            last_percent = -1
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    percent = int(status.progress() * 100)
                    if percent >= last_percent + 10 or done:
                        print(f"Downloading current Drive revision: {percent}%", flush=True)
                        last_percent = percent
        except Exception:
            temporary.close()
            path.unlink(missing_ok=True)
            raise
        temporary.close()
        expected_size = int(metadata["size"]) if metadata.get("size") else None
        if expected_size is not None and path.stat().st_size != expected_size:
            path.unlink(missing_ok=True)
            raise IOError("Downloaded Drive file size does not match its metadata")
        atexit.register(path.unlink, missing_ok=True)
        return path
