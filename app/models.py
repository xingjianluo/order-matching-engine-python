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
    INVALID_LIMIT = "INVALID_LIMIT"


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
class PositionRiskView:
    """单品种风险视图（当前值 / 全部在途订单按委托价成交后的预计值）。"""
    symbol: str
    position: Decimal                    # 当前净持仓
    reserved_sell: Decimal               # 在途卖单占用数量
    available_position: Decimal          # 可卖数量 = position - reserved_sell
    max_position: Decimal | None         # 该品种净持仓绝对值上限（None 表示不限）
    projected_position: Decimal          # 在途买卖单全部成交后的净持仓
    position_limit_breached: bool        # 当前 |position| 是否超限
    projected_position_limit_breached: bool  # 在途全部成交后是否超限


@dataclass
class AccountRiskSnapshot:
    """账户组合级风险快照：同一把锁内生成，代表一个一致时点。"""
    account_id: str
    cash: Decimal                        # 当前现金
    reserved_buy_notional: Decimal       # 跨品种全部在途买单占用名义额
    available_cash: Decimal              # 可用现金 = cash - reserved_buy_notional
    projected_cash: Decimal              # 在途买单全部成交后的现金
    positions: dict[str, PositionRiskView]
    # 单笔名义额上限
    max_order_notional: Decimal
    # 超过单笔名义额上限的在途订单（限额收紧后可能出现）
    order_notional_breaches: list[str] = field(default_factory=list)
    cash_limit_breached: bool = False            # 当前现金是否为负
    projected_cash_limit_breached: bool = False  # 在途买单全部成交后现金是否为负
    any_breach: bool = False                     # 上述任一当前/预计违约标志
