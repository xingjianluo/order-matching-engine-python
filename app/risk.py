"""账户级风控：组合资金占用、分品种持仓占用、限额与单笔名义额校验。

约定（由服务层在调用前汇总，均不含正在预检的新订单）：
- ``reserved_sell_qty``：该账户在该品种上全部在途卖单的剩余数量；
- ``reserved_buy_qty``：该账户在该品种上全部在途买单的剩余数量；
- ``total_reserved_buy_notional``：该账户在**所有品种**上在途买单占用的
  名义额（剩余量 × 委托价）之和——同一份现金跨品种共享，不能按品种各算一份。

判定口径：
- 可用现金 = cash - total_reserved_buy_notional；
- 可用持仓（可卖数量）= position - reserved_sell_qty；
- 预计净持仓 = position + reserved_buy_qty - reserved_sell_qty，再叠加新单，
  用于判断“在途订单全部成交后”是否突破品种持仓限额。
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
        total_reserved_buy_notional: Decimal,
        reserved_buy_qty: Decimal = Decimal(0),
    ) -> None:
        notional = order.price * order.quantity
        limit = account.max_position.get(order.symbol)
        position = account.positions.get(order.symbol, Decimal(0))

        if order.side == Side.SELL:
            # 先区分：连总持仓都不足 vs 可用持仓（扣除在途卖单）不足
            if order.quantity > position:
                raise TradingError(
                    RejectReason.INSUFFICIENT_POSITION,
                    f"sell qty {order.quantity} exceeds position {position} "
                    f"of {order.symbol}",
                )
            available = position - reserved_sell_qty
            if order.quantity > available:
                raise TradingError(
                    RejectReason.INSUFFICIENT_POSITION,
                    f"sell qty {order.quantity} exceeds available {available} "
                    f"(position {position} minus {reserved_sell_qty} reserved)",
                )
            # 组合口径：本品种在途买单会增加、在途卖单会减少净持仓
            projected = position + reserved_buy_qty - reserved_sell_qty - order.quantity
            if limit is not None and abs(projected) > limit:
                raise TradingError(
                    RejectReason.RISK_LIMIT_EXCEEDED,
                    f"projected net position {projected} (abs {abs(projected)}) "
                    f"exceeds limit {limit} on {order.symbol}",
                )
        else:
            # 组合口径：现金占用跨所有品种汇总
            available_cash = account.cash - total_reserved_buy_notional
            if notional > available_cash:
                raise TradingError(
                    RejectReason.INSUFFICIENT_CASH,
                    f"buy notional {notional} exceeds available cash "
                    f"{available_cash} (cash {account.cash} reserved "
                    f"{total_reserved_buy_notional} across symbols)")
            projected = position + reserved_buy_qty + order.quantity - reserved_sell_qty
            if limit is not None and abs(projected) > limit:
                raise TradingError(
                    RejectReason.RISK_LIMIT_EXCEEDED,
                    f"projected net position {projected} exceeds limit {limit} "
                    f"on {order.symbol}")

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
