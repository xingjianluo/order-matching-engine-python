"""本地仅追加事件日志（JSON Lines）。

- 每条事件占一行 JSON，append 后 fsync，保证进程崩溃后已确认事件不丢失。
- read_all 顺序读取；空行跳过，JSON 损坏或缺少必填字段时明确抛出
  CORRUPT_EVENT，而不是静默跳过。
- 序号连续性由上层 service 校验，本层只保证字节流忠实读出。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

from app.models import RejectReason, TradingError

REQUIRED_FIELDS = {"seq", "type"}


class EventLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None

    def _handle(self):
        if self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8")
        return self._fh

    def append(self, event: dict[str, Any]) -> None:
        fh = self._handle()
        fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())

    def read_all(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TradingError(
                        RejectReason.CORRUPT_EVENT,
                        f"invalid JSON at {self.path}:{lineno}: {exc}",
                    ) from exc
                if not isinstance(event, dict) or not REQUIRED_FIELDS.issubset(event):
                    raise TradingError(
                        RejectReason.CORRUPT_EVENT,
                        f"event missing required fields {REQUIRED_FIELDS} "
                        f"at {self.path}:{lineno}",
                    )
                yield event

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
