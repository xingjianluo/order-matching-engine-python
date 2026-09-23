"""可区分的业务与重放错误。"""
from __future__ import annotations

from app.models import RejectReason

# 拒绝原因 -> HTTP 状态码，供接口层统一映射
REASON_HTTP_STATUS: dict[RejectReason, int] = {}


class TradingError(Exception):
    """所有可预期业务拒绝的基类。"""

    reason: RejectReason = RejectReason.INVALID_REQUEST
    http_status: int = 400

    def __init__(self, message: str = "", *, reason: RejectReason | None = None):
        super().__init__(message or self.__class__.__name__)
        if reason is not None:
            self.reason = reason


class UnknownAccountError(TradingError):
    reason = RejectReason.UNKNOWN_ACCOUNT
    http_status = 404


class UnknownOrderError(TradingError):
    reason = RejectReason.UNKNOWN_ORDER
    http_status = 404


class OrderNotActiveError(TradingError):
    reason = RejectReason.ORDER_NOT_ACTIVE
    http_status = 409


class AlreadyCancelledError(TradingError):
    reason = RejectReason.ALREADY_CANCELLED
    http_status = 409


class SelfTradePreventedError(TradingError):
    reason = RejectReason.SELF_TRADE
    http_status = 409


class RiskRejectedError(TradingError):
    http_status = 422


class OrderNotionalExceededError(RiskRejectedError):
    reason = RejectReason.RISK_ORDER_NOTIONAL_EXCEEDED


class NetPositionExceededError(RiskRejectedError):
    reason = RejectReason.RISK_NET_POSITION_EXCEEDED


class InsufficientCashError(TradingError):
    reason = RejectReason.INSUFFICIENT_CASH
    http_status = 422


class InsufficientPositionError(TradingError):
    reason = RejectReason.INSUFFICIENT_POSITION
    http_status = 422


class IdempotencyConflictError(TradingError):
    reason = RejectReason.IDEMPOTENCY_CONFLICT
    http_status = 409


class ReplayError(Exception):
    """重放期间发现非法或乱序事件。

    明确抛出而不是静默跳过：启动即失败，避免以错误状态对外服务。
    """


def _build_reason_status_map() -> dict[RejectReason, int]:
    found: dict[RejectReason, int] = {RejectReason.INVALID_REQUEST: 400}
    for obj in globals().values():
        if isinstance(obj, type) and issubclass(obj, TradingError) and obj is not TradingError:
            reason = getattr(obj, "reason", RejectReason.INVALID_REQUEST)
            # 跳过未声明具体 reason 的中间基类（如 RiskRejectedError）
            if reason == RejectReason.INVALID_REQUEST:
                continue
            found.setdefault(reason, getattr(obj, "http_status", 400))
    return found


REASON_HTTP_STATUS.update(_build_reason_status_map())
