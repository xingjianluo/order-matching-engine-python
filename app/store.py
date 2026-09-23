"""本地仅追加（append-only）JSONL 事件存储。

- append：分配单调递增 seq，fsync 落盘后返回事件，保证可恢复。
- replay：严格按行读出并校验基本信封结构；业务层乱序/语义校验由引擎负责。
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from app.errors import ReplayError
from app.models import Event, EventType


class EventStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # touch 文件，保证首次 replay 也能打开
        self.path.touch(exist_ok=True)

    def append(self, event_type: EventType, payload: dict, *, timestamp: str | None = None) -> Event:
        """追加一行事件。调用方（引擎）已持锁，因此这里 seq 分配无需额外锁。"""
        next_seq = self._current_seq() + 1
        event = Event(
            seq=next_seq,
            type=EventType(event_type),
            timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
            payload=payload,
        )
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return event

    def replay(self) -> Iterator[Event]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                text = raw.strip()
                if not text:
                    raise ReplayError(f"第 {line_no} 行为空：事件日志不允许出现空行")
                yield self._decode_line(text, line_no=line_no)

    def _current_seq(self) -> int:
        """取最后一行的 seq；空文件返回 0。"""
        last = ""
        with self.path.open("rb") as fh:
            # 简单回退读取最后一行；本地单进程，文件不大时足够
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return 0
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") < 2:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
            lines = [ln for ln in data.splitlines() if ln.strip()]
            if lines:
                last = lines[-1].decode("utf-8")
        try:
            return int(json.loads(last)["seq"])
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise ReplayError("无法解析事件日志最后一行的 seq，文件可能损坏") from exc

    @staticmethod
    def _decode_line(line: str, *, line_no: int) -> Event:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayError(f"第 {line_no} 行不是合法 JSON：{exc}") from exc
        try:
            event = Event.model_validate(obj)
        except Exception as exc:  # pydantic 校验错误
            raise ReplayError(f"第 {line_no} 行事件信封非法：{exc}") from exc
        if event.seq <= 0:
            raise ReplayError(f"第 {line_no} 行 seq={event.seq} 非法：必须为正整数")
        return event
