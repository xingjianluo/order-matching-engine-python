"""领域类型与对外 DTO（仅类型定义层）。

金额/价格/数量统一使用 Decimal，避免浮点误差；网络层以字符串传输。
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    NEW = "NEW"                  # 已入簿、尚未成交
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class EventType(str, Enum):
    ACCOUNT_CONFIGURED = "ACCOUNT_CONFIGURED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_REJECTED = "ORDER_REJECTED"
    TRADE_EXECUTED = "TRADE_EXECUTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"


class RejectReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
    RISK_ORDER_NOTIONAL_EXCEEDED = "RISK_ORDER_NOTIONAL_EXCEEDED"
    RISK_NET_POSITION_EXCEEDED = "RISK_NET_POSITION_EXCEEDED"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    SELF_TRADE = "SELF_TRADE"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    ORDER_NOT_ACTIVE = "ORDER_NOT_ACTIVE"
    ALREADY_CANCELLED = "ALREADY_CANCELLED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


class AccountConfig(BaseModel):
    account_id: str
    cash: Decimal = Field(default=Decimal(0))
    position: Decimal = Field(default=Decimal(0))
    max_order_notional: Decimal | None = None
    max_net_position: Decimal | None = None


class SubmitOrderRequest(BaseModel):
    account_id: str
    side: Side
    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    idempotency_key: str | None = None


class CancelOrderRequest(BaseModel):
    idempotency_key: str | None = None


class OrderView(BaseModel):
    order_id: str
    account_id: str
    side: Side
    price: Decimal
    original_qty: Decimal
    filled_qty: Decimal
    leaves_qty: Decimal
    status: OrderStatus


class TradeView(BaseModel):
    trade_id: str
    order_id: str
    counter_order_id: str
    maker_account_id: str
    taker_account_id: str
    side: Side               # taker 方向
    price: Decimal
    quantity: Decimal


class AccountView(BaseModel):
    account_id: str
    cash: Decimal
    reserved_cash: Decimal
    position: Decimal
    reserved_sell_qty: Decimal
    max_order_notional: Decimal | None = None
    max_net_position: Decimal | None = None


class SubmitResponse(BaseModel):
    accepted: bool
    order_id: str | None = None
    idempotency_key: str | None = None
    idempotent_hit: bool = False
    status: OrderStatus | None = None
    trades: list[TradeView] = Field(default_factory=list)
    reject_reason: RejectReason | None = None


class CancelResponse(BaseModel):
    cancelled: bool
    order_id: str
    idempotency_key: str | None = None
    idempotent_hit: bool = False
    reason: RejectReason | None = None


class Event(BaseModel):
    """持久化事件信封：seq 严格递增，重放时据此校验。"""
    seq: int
    type: EventType
    timestamp: str
    payload: dict


# ---- 事件 payload 模型（用于校验落盘/重放数据合法性） ----
class AccountConfiguredPayload(BaseModel):
    account_id: str
    cash: Decimal
    position: Decimal
    max_order_notional: Decimal | None = None
    max_net_position: Decimal | None = None


class OrderSubmittedPayload(BaseModel):
    order_id: str
    account_id: str
    side: Side
    price: Decimal
    original_qty: Decimal
    idempotency_key: str | None = None


class OrderRejectedPayload(BaseModel):
    account_id: str
    side: Side
    price: Decimal
    quantity: Decimal
    reason: RejectReason
    idempotency_key: str | None = None


class TradeExecutedPayload(BaseModel):
    trade_id: str
    maker_order_id: str
    taker_order_id: str
    maker_account_id: str
    taker_account_id: str
    taker_side: Side
    price: Decimal
    quantity: Decimal


class OrderCancelledPayload(BaseModel):
    order_id: str
    idempotency_key: str | None = None
