"""领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有业务规则违例的基类。"""


class AuthorizationError(DomainError):
    """操作者无权执行该动作（如协会试图独自批准本方例外）。"""


class IneligibleError(DomainError):
    """候选人不满足岗位要求，且没有覆盖相应条款的已批准例外。"""

    def __init__(self, failures: list["tuple[str, str]"]):
        self.failures = failures
        super().__init__("；".join(f"[{code}] {detail}" for code, detail in failures))


class ConflictStateError(DomainError):
    """当前状态不允许该动作（岗位已暂停、邀请已终局等）。"""
