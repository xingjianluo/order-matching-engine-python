"""账户级风控：持仓限额、可用持仓/资金与单笔名义额校验。

约定（由服务层在调用前汇总）：
- ``reserved_sell``：该账户在该品种上已挂卖单占用的数量；
- ``reserved_buy_notional``：该账户在该品种上已挂买单占用的名义额；
  可用 = 当前持仓 - reserved_sell；可用资金 = cash - 全部已挂买单占用名义额。
风控只做判定，不修改状态，保证拒绝不产生副作用。
"""
from __future__ import annotations

from decimal import Decimal

from app.models import Account, Order, RejectReason, Side, TradingError


class RiskManager:
    def check_order(
        self,
        account: Account,
        order: Order,
        reserved_sell: Decimal,
        reserved_buy_notional: Decimal,
    ) -> None:
        notional = order.price * order.quantity
        limit = account.max_position.get(order.symbol)

        if order.side == Side.SELL:
            position = account.positions.get(order.symbol, Decimal(0))
            available = position - reserved_sell
            # 先区分：连持仓都不足 vs 超过风控限额
            if order.quantity > position:
                raise TradingError(
                    RejectReason.INSUFFICIENT_POSITION,
                    f"sell qty {order.quantity} exceeds position {position} "
                    f"of {order.symbol}",
                )
            if order.quantity > available:
                raise TradingError(
                    RejectReason.INSUFFICIENT_POSITION,
                    f"sell qty {order.quantity} exceeds available {available} "
                    f"(position minus {reserved_sell} reserved)",
                )
            projected = abs(position - order.quantity)
            if limit is not None and projected > limit:
                raise TradingError(
                    RejectReason.RISK_LIMIT_EXCEEDED,
                    f"projected short exposure {projected} exceeds limit {limit}",
                )
        else:
            available_cash = account.cash - reserved_buy_notional
            if notional > available_cash:
                raise TradingError(
                    RejectReason.INSUFFICIENT_CASH,
                    f"buy notional {notional} exceeds available cash "
                    f"{available_cash} (cash {account.cash} reserved "
                    f"{reserved_buy_notional})")
            position = account.positions.get(order.symbol, Decimal(0))
            projected = abs(position + order.quantity)
            if limit is not None and projected > limit:
                raise TradingError(
                    RejectReason.RISK_LIMIT_EXCEEDED,
                    f"projected position {projected} exceeds limit {limit}")

        if notional > account.max_order_notional:
            raise TradingError(
                RejectReason.RISK_LIMIT_EXCEEDED,
                f"order notional {notional} exceeds per-order cap "
                f"{account.max_order_notional}",
            )

    def check_self_trade(self, incoming: Order, resting: Order) -> None:
        if incoming.account == resting.account:
            raise TradingError(
                RejectReason.SELF_TRADE,
                f"order {incoming.order_id} would self-trade with "
                f"{resting.order_id} under account {incoming.account}",
            )
