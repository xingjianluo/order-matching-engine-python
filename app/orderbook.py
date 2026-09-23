"""单品种订单簿：价格优先、时间优先撮合，允许部分成交。

设计要点：
- 买盘按价格降序、卖盘按价格升序优先；同一价位上先进入队列者先成交（FIFO）。
- ``plan_match`` 只做撮合规划、不修改任何状态，便于上层先做自成交与风控预检，
  保证被拒绝的请求绝不改变订单簿。
- ``place`` / ``commit`` / ``remove`` 才产生状态变更。
"""
from __future__ import annotations

from decimal import Decimal

from app.models import Order, OrderStatus, Side


class OrderBook:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self._bids: dict[Decimal, list[Order]] = {}   # price -> FIFO 队列
        self._asks: dict[Decimal, list[Order]] = {}
        self._index: dict[str, Order] = {}

    # ---------- 查询 ----------
    def get(self, order_id: str) -> Order | None:
        return self._index.get(order_id)

    def bids(self) -> list[Order]:
        out: list[Order] = []
        for price in sorted(self._bids.keys(), reverse=True):
            out.extend(o for o in self._bids[price] if o.is_active)
        return out

    def asks(self) -> list[Order]:
        out: list[Order] = []
        for price in sorted(self._asks.keys()):
            out.extend(o for o in self._asks[price] if o.is_active)
        return out

    def best_bid(self) -> Decimal | None:
        active = [p for p, q in self._bids.items() if any(o.is_active for o in q)]
        return max(active) if active else None

    def best_ask(self) -> Decimal | None:
        active = [p for p, q in self._asks.items() if any(o.is_active for o in q)]
        return min(active) if active else None

    def snapshot(self) -> dict:
        def agg(levels: dict[Decimal, list[Order]]) -> list[dict]:
            rows = []
            for price, orders in levels.items():
                qty = sum((o.remaining for o in orders if o.is_active), Decimal(0))
                if qty > 0:
                    rows.append({"price": str(price), "quantity": str(qty),
                                 "orders": [o.order_id for o in orders if o.is_active]})
            return rows
        return {
            "symbol": self.symbol,
            "bids": sorted(agg(self._bids), key=lambda r: Decimal(r["price"]), reverse=True),
            "asks": sorted(agg(self._asks), key=lambda r: Decimal(r["price"])),
        }

    # ---------- 撮合（纯函数式规划） ----------
    def plan_match(self, order: Order) -> list[tuple[Order, Decimal]]:
        """返回 [(对手订单, 成交量), ...]，不修改任何状态。成交价取挂单价。"""
        plan: list[tuple[Order, Decimal]] = []
        remaining = order.remaining
        if order.side == Side.BUY:
            for price in sorted(self._asks.keys()):
                if price > order.price or remaining <= 0:
                    break
                for resting in list(self._asks[price]):
                    if not resting.is_active:
                        continue
                    if remaining <= 0:
                        break
                    qty = min(remaining, resting.remaining)
                    if qty > 0:
                        plan.append((resting, qty))
                        remaining -= qty
        else:
            for price in sorted(self._bids.keys(), reverse=True):
                if price < order.price or remaining <= 0:
                    break
                for resting in list(self._bids[price]):
                    if not resting.is_active:
                        continue
                    if remaining <= 0:
                        break
                    qty = min(remaining, resting.remaining)
                    if qty > 0:
                        plan.append((resting, qty))
                        remaining -= qty
        return plan

    # ---------- 状态变更 ----------
    def place(self, order: Order) -> None:
        """将订单（含未成交剩余量）放入簿中。"""
        levels = self._bids if order.side == Side.BUY else self._asks
        levels.setdefault(order.price, []).append(order)
        self._index[order.order_id] = order

    def commit(self, order: Order, plan: list[tuple[Order, Decimal]]) -> list[Order]:
        """按撮合计划更新数量与状态；吃单有剩余则入簿。返回全部参与订单。"""
        touched: list[Order] = []
        for resting, qty in plan:
            resting.filled_qty += qty
            resting.status = (OrderStatus.FILLED if resting.remaining == 0
                              else OrderStatus.PARTIALLY_FILLED)
            touched.append(resting)
        matched = sum((q for _, q in plan), Decimal(0))
        order.filled_qty += matched
        if order.remaining > 0:
            order.status = (OrderStatus.PARTIALLY_FILLED if order.filled_qty > 0
                            else OrderStatus.NEW)
            self.place(order)
        else:
            order.status = OrderStatus.FILLED
        touched.append(order)
        self._gc_levels()
        return touched

    def remove(self, order_id: str) -> Order:
        """从簿中移除仍有效的订单；订单不存在或已不在簿中时抛 KeyError。"""
        order = self._index.pop(order_id)
        levels = self._bids if order.side == Side.BUY else self._asks
        queue = levels[order.price]
        queue.remove(order)
        if not queue:
            del levels[order.price]
        order.status = OrderStatus.CANCELLED
        return order

    def apply_fill(self, order_id: str, qty: Decimal) -> Order:
        """重放成交事件时更新簿内订单数量；完全成交则移出队列。"""
        order = self._index[order_id]
        order.filled_qty += qty
        if order.remaining == 0:
            order.status = OrderStatus.FILLED
            levels = self._bids if order.side == Side.BUY else self._asks
            queue = levels[order.price]
            queue.remove(order)
            if not queue:
                del levels[order.price]
            self._index.pop(order_id, None)
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        return order

    def _gc_levels(self) -> None:
        for levels in (self._bids, self._asks):
            for price in list(levels.keys()):
                q = levels[price]
                dead = [o for o in q if not o.is_active]
                for o in dead:
                    q.remove(o)
                    self._index.pop(o.order_id, None)
                if not q:
                    del levels[price]
