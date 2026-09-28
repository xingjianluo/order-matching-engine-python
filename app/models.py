"""领域模型：订单、成交、账户与错误类型。

数量与价格全程使用 Decimal，避免二进制浮点误差，保证数量守恒断言成立。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    NEW = "NEW"                       # 已进入订单簿，等待成交
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"            # 未进入订单簿（风控/自成交等拒绝）


class RejectReason(str, Enum):
    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    RISK_LIMIT_EXCEEDED = "RISK_LIMIT_EXCEEDED"
    SELF_TRADE = "SELF_TRADE"
    DUPLICATE_IDEMPOTENCY_KEY = "DUPLICATE_IDEMPOTENCY_KEY"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    ORDER_ALREADY_FILLED = "ORDER_ALREADY_FILLED"
    ORDER_ALREADY_CANCELLED = "ORDER_ALREADY_CANCELLED"
    CORRUPT_EVENT = "CORRUPT_EVENT"
    OUT_OF_ORDER_EVENT = "OUT_OF_ORDER_EVENT"


class TradingError(Exception):
    """所有可预期的业务错误，携带可区分的拒绝原因。"""

    def __init__(self, reason: RejectReason, message: str = ""):
        self.reason = reason
        super().__init__(message or reason.value)


@dataclass
class Order:
    order_id: str
    client_order_id: str            # 幂等标识
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal               # 原始委托数量
    account: str
    filled_qty: Decimal = Decimal(0)
    status: OrderStatus = OrderStatus.NEW
    sequence: int = 0               # 进入订单簿的全局序号，决定时间优先
    reject_reason: RejectReason | None = None

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled_qty

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


@dataclass
class Fill:
    fill_id: str
    sequence: int                   # 成交事件全局序号
    symbol: str
    price: Decimal                  # 成交价=挂单价（吃单方吃到对手价）
    quantity: Decimal
    buy_order_id: str
    sell_order_id: str
    buy_account: str
    sell_account: str
    taker_order_id: str


@dataclass
class Account:
    account_id: str
    cash: Decimal = Decimal(0)
    # symbol -> 净持仓（买入为正，卖出为负）
    positions: dict[str, Decimal] = field(default_factory=dict)
    # 风控限额：单品种最大净持仓绝对值
    max_position: dict[str, Decimal] = field(default_factory=dict)
    # 单笔订单最大名义额（价*量）
    max_order_notional: Decimal = Decimal("Infinity")


@dataclass
class SymbolRiskSnapshot:
    """单品种风险占用视图（同一账户视角）。

    - position：当前净持仓（买正卖负）。
    - reserved_sell：该品种全部在途卖单的剩余数量合计（占用可卖持仓）。
    - available_position：当前持仓 - 在途卖单占用，即可再卖数量。
    - resting_buy_qty：该品种全部在途买单剩余数量合计（用于预估成交后持仓）。
    - projected_position：在途买卖单全部成交后的预估净持仓
      （= position + resting_buy_qty - reserved_sell，同账户买卖不会互相对手）。
    - max_position：当前生效的持仓限额（无配置为 None）。
    - limit_breached / projected_limit_breached：当前/预估是否已严格超限。
    """

    symbol: str
    position: Decimal
    reserved_sell: Decimal
    available_position: Decimal
    resting_buy_qty: Decimal
    projected_position: Decimal
    max_position: Decimal | None
    limit_breached: bool
    projected_limit_breached: bool


@dataclass
class AccountRiskSnapshot:
    """账户组合级风险快照：跨品种共享现金、按品种分列持仓占用。

    - reserved_buy_notional：全部品种在途买单剩余名义额合计，统一占用现金。
    - available_cash：cash - reserved_buy_notional，即新买单真正还能用的现金。
    - projected_cash：在途买卖单全部成交后的预估现金
      （买单花掉 remaining*price、卖单收入 remaining*price）。
    - cash_breached：当前现金是否已为负（正常风控下不应出现，仅作观测）。
    - projected_cash_breached：预估成交后现金是否为负（正常风控下不应出现）。
    - projected_limit_breached / current_limit_breached：任一品种预估/当前超限。
    - over_capped_orders：名义额已超过当前单笔上限的在途订单
      （收紧限额后用于提示，不强制撤单）。
    """

    account_id: str
    cash: Decimal
    reserved_buy_notional: Decimal
    available_cash: Decimal
    max_order_notional: Decimal
    projected_cash: Decimal
    cash_breached: bool
    projected_cash_breached: bool
    current_limit_breached: bool
    projected_limit_breached: bool
    over_capped_orders: list[str] = field(default_factory=list)
    symbols: dict[str, SymbolRiskSnapshot] = field(default_factory=dict)
