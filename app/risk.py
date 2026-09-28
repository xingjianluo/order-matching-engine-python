"""账户组合级风控：现金/持仓占用、持仓限额与单笔名义额校验。

占用口径（由服务层在调用前从全部在途订单汇总）：
- ``reserved_sell_qty``：该账户在**该品种**上全部在途卖单的剩余数量合计，
  按品种分别占用可卖持仓；可用持仓 = 当前持仓 - reserved_sell_qty。
- ``reserved_buy_notional``：该账户在**全部品种**上在途买单剩余名义额合计，
  跨品种统一占用同一份现金；可用现金 = cash - reserved_buy_notional。
- ``resting_buy_qty``：该品种全部在途买单剩余数量合计，仅用于限额预估。
持仓限额按“该品种在途买卖单全部成交后的预估净持仓”判定；
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
        reserved_sell_qty: Decimal,
        reserved_buy_notional: Decimal,
        resting_buy_qty: Decimal,
    ) -> None:
        notional = order.price * order.quantity
        limit = account.max_position.get(order.symbol)
        position = account.positions.get(order.symbol, Decimal(0))

        if order.side == Side.SELL:
            available = position - reserved_sell_qty
            # 先区分：连总持仓都不足 vs 可用（扣除在途卖单）不足
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
                    f"(position {position} minus {reserved_sell_qty} reserved)",
                )
            # 该品种在途买卖单全部成交后，净持仓的预估值
            projected = abs(
                position + resting_buy_qty
                - reserved_sell_qty - order.quantity
            )
            if limit is not None and projected > limit:
                raise TradingError(
                    RejectReason.RISK_LIMIT_EXCEEDED,
                    f"projected net position {projected} exceeds limit {limit}",
                )
        else:
            # 跨品种共享现金：扣减全部品种在途买单占用
            available_cash = account.cash - reserved_buy_notional
            if notional > available_cash:
                raise TradingError(
                    RejectReason.INSUFFICIENT_CASH,
                    f"buy notional {notional} exceeds available cash "
                    f"{available_cash} (cash {account.cash} reserved "
                    f"{reserved_buy_notional} across all symbols)")
            projected = abs(
                position + resting_buy_qty + order.quantity
                - reserved_sell_qty
            )
            if limit is not None and projected > limit:
                raise TradingError(
                    RejectReason.RISK_LIMIT_EXCEEDED,
                    f"projected net position {projected} exceeds limit {limit}")

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
