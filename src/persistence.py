"""秘书处状态的持久化与恢复。

服务可能中断。为保证秘书处恢复后仍能解释每次指派、续办全部待办，
:class:`~src.secretariat.Secretariat` 的全部状态可序列化为 JSON 落盘，
恢复后对象身份、版本号、待办历史与 append-only 审计序号保持连续。

编码采用带类型标签的 JSON：数据类标 ``__dc__``，枚举标 ``__enum__``，
日期时间分别标 ``__date__`` / ``__dt__``，集合标 ``__set__``。
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from . import model
from .secretariat import Secretariat

_DC_CLASSES = {
    cls.__name__: cls
    for cls in (
        getattr(model, name) for name in dir(model)
    )
    if dataclasses.is_dataclass(cls)
}


def encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "__dc__": type(value).__name__,
            "fields": {
                f.name: encode(getattr(value, f.name))
                for f in dataclasses.fields(value)
            },
        }
    if isinstance(value, dict):
        return {"__dict__": {k: encode(v) for k, v in value.items()}}
    if isinstance(value, tuple):
        return {"__tuple__": [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, set):
        return {"__set__": [encode(v) for v in value]}
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, Enum):
        return {"__enum__": f"{type(value).__name__}.{value.name}"}
    return value


def decode(value: Any) -> Any:
    if isinstance(value, dict):
        if "__dc__" in value:
            cls = _DC_CLASSES[value["__dc__"]]
            return cls(**{k: decode(v) for k, v in value["fields"].items()})
        if "__dict__" in value:
            return {k: decode(v) for k, v in value["__dict__"].items()}
        if "__set__" in value:
            return {decode(v) for v in value["__set__"]}
        if "__tuple__" in value:
            return tuple(decode(v) for v in value["__tuple__"])
        if "__dt__" in value:
            return datetime.fromisoformat(value["__dt__"])
        if "__date__" in value:
            return date.fromisoformat(value["__date__"])
        if "__enum__" in value:
            class_name, member = value["__enum__"].split(".")
            return getattr(getattr(model, class_name), member)
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def save_state(secretariat: Secretariat, path: str | Path) -> None:
    payload = {
        "rest_hours": secretariat.rest_hours,
        "cert_expiry_days": secretariat.cert_expiry_days,
        "visa_expiry_days": secretariat.visa_expiry_days,
        "seq": secretariat._seq,
        "associations": list(secretariat.associations.values()),
        "officials": list(secretariat.officials.values()),
        "events": list(secretariat.events.values()),
        "invitations": list(secretariat.invitations.values()),
        "exceptions": list(secretariat.exceptions.values()),
        "evaluations": list(secretariat.evaluations.values()),
        "appeals": list(secretariat.appeals.values()),
        "todos": list(secretariat.todos.values()),
        "withdrawals": secretariat.withdrawals,
        "audit_log": secretariat.audit_log,
    }
    Path(path).write_text(
        json.dumps(encode(payload), ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def load_state(path: str | Path) -> Secretariat:
    payload = decode(json.loads(Path(path).read_text(encoding="utf-8")))
    secretariat = Secretariat(
        rest_hours=payload["rest_hours"],
        cert_expiry_days=payload["cert_expiry_days"],
        visa_expiry_days=payload["visa_expiry_days"],
    )
    for association in payload["associations"]:
        secretariat.associations[association.id] = association
    for official in payload["officials"]:
        secretariat.officials[official.id] = official
    for event in payload["events"]:
        secretariat.events[event.id] = event
    for invitation in payload["invitations"]:
        secretariat.invitations[invitation.id] = invitation
    for grant in payload["exceptions"]:
        secretariat.exceptions[grant.id] = grant
    for evaluation in payload["evaluations"]:
        secretariat.evaluations[evaluation.id] = evaluation
    for appeal in payload["appeals"]:
        secretariat.appeals[appeal.id] = appeal
    for todo in payload["todos"]:
        secretariat.todos[todo.id] = todo
    secretariat.withdrawals = payload["withdrawals"]
    secretariat.audit_log = payload["audit_log"]
    secretariat._seq = payload["seq"]
    return secretariat
