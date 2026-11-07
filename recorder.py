"""遊んでいるあいだの数値を、CSV または JSON のファイルへ追記します。"""

from __future__ import annotations

import csv
import json
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


class SessionRecorder:
    def __init__(self, folder: Path, kind: str) -> None:
        if kind not in {"csv", "json"}:
            raise ValueError(f"未対応の形式です: {kind}")
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        extension = "csv" if kind == "csv" else "jsonl"
        self.path = folder / f"playlog_{stamp}.{extension}"
        self.kind = kind
        self.count = 0
        encoding = "utf-8-sig" if kind == "csv" else "utf-8"
        self._file = self.path.open("w", encoding=encoding, newline="\n")
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
