"""遊んでいるあいだの数値を、CSV または JSON のファイルへ追記します。"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime
from pathlib import Path

FIELDS = [
    "時刻",
    "対象ソフト",
    "FPS",
    "生成前FPS",
    "GPU稼働率",
    "GPU温度",
    "GPUホットスポット",
    "CPU稼働率",
    "CPU温度",
    "使用メモリMB",
    "メモリ使用率",
]


def _open_new_log(folder: Path, extension: str, encoding: str):
    """同じ秒に記録を始めても、前のファイルを消さないようにします。"""
    for _ in range(5):
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
        token = uuid.uuid4().hex[:8]
        path = folder / f"playlog_{stamp}_{token}.{extension}"
        try:
            handle = path.open("x", encoding=encoding, newline="\n")
        except FileExistsError:
            continue
        return path, handle
    raise FileExistsError("記録ファイルを新しく作れませんでした。")


class SessionRecorder:
    def __init__(self, folder: Path, kind: str) -> None:
        if kind not in {"csv", "json"}:
            raise ValueError(f"未対応の形式です: {kind}")
        folder.mkdir(parents=True, exist_ok=True)
        extension = "csv" if kind == "csv" else "jsonl"
        self.kind = kind
        self.count = 0
        encoding = "utf-8-sig" if kind == "csv" else "utf-8"
        self.path, self._file = _open_new_log(folder, extension, encoding)
        self._writer: csv.DictWriter | None = None
        if kind == "csv":
            self._writer = csv.DictWriter(self._file, fieldnames=FIELDS)
            self._writer.writeheader()
            self._file.flush()

    def write(self, row: dict) -> None:
        if self.kind == "csv":
            assert self._writer is not None
            prepared = {
                key: "" if row.get(key) is None else row.get(key) for key in FIELDS
            }
            self._writer.writerow(prepared)
        else:
            payload = {key: row.get(key) for key in FIELDS}
            self._file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._file.flush()
        self.count += 1

    def close(self) -> None:
        self._file.close()
