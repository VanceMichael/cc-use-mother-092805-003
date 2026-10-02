"""只增事件存储：追加 JSONL、按序号重放。

系统状态完全由事件流导出；服务中断后重新载入即可恢复，
每次决定所依据的材料版本也在事件中留痕，可随时重放解释。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    at: datetime
    actor: str
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(
            {
                "seq": self.seq,
                "type": self.type,
                "at": self.at.isoformat(),
                "actor": self.actor,
                "payload": self.payload,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> "Event":
        raw = json.loads(line)
        return cls(
            seq=int(raw["seq"]),
            type=raw["type"],
            at=datetime.fromisoformat(raw["at"]),
            actor=raw["actor"],
            payload=raw["payload"],
        )


class EventStore:
    """文件后端的事件存储；传 path=None 时仅驻留内存（便于测试）。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._events: list[Event] = []
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    event = Event.from_json(line)
                    if event.seq != len(self._events) + 1:
                        raise ValueError("事件序号不连续，存储可能已损坏")
                    self._events.append(event)

    @property
    def next_seq(self) -> int:
        return len(self._events) + 1

    def append(self, type_: str, actor: str, payload: dict[str, Any], at: datetime) -> Event:
        event = Event(len(self._events) + 1, type_, at, actor, payload)
        self._events.append(event)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(event.to_json() + "\n")
        return event

    def replay(self, handler: Callable[[Event], None]) -> None:
        for event in list(self._events):
            handler(event)

    def events(self) -> Iterable[Event]:
        return list(self._events)
