"""限价单撮合 + 账户风控的内存状态机，状态只能由事件 apply 得到。

并发模型：所有写操作在同一把可重入锁内完成「校验 → 事件落盘(fsync) → apply」，
因此并发提交/撤单/查询不会产生交错的中间状态。

撮合规则：价格优先、时间优先；允许部分成交；允许跨价（吃单），成交价取 resting
（maker）价格。自成交：incoming 订单入簿前扫描对手方向同账户可交叉订单，命中即整单拒绝。
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.errors import (
    AlreadyCancelledError,
    IdempotencyConflictError,
    InsufficientCashError,
    InsufficientPositionError,
    NetPositionExceededError,
    OrderNotActiveError,
    OrderNotionalExceededError,
    ReplayError,
    SelfTradePreventedError,
    TradingError,
    UnknownAccountError,
    UnknownOrderError,
)
from app.models import (
    AccountConfig,
    AccountConfiguredPayload,
    AccountView,
    CancelResponse,
    Event,
    EventType,
    OrderCancelledPayload,
    OrderRejectedPayload,
    OrderStatus,
    OrderSubmittedPayload,
    OrderView,
    RejectReason,
    Side,
    SubmitOrderRequest,
    SubmitResponse,
    TradeExecutedPayload,
    TradeView,
)
from app.store import EventStore

logger = logging.getLogger("ome.engine")


@dataclass
class OrderState:
    order_id: str
    account_id: str
    side: Side
    price: Decimal
    original_qty: Decimal
    filled_qty: Decimal = Decimal(0)
    status: OrderStatus = OrderStatus.NEW

    @property
    def leaves_qty(self) -> Decimal:
        return self.original_qty - self.filled_qty


@dataclass
class AccountState:
    account_id: str
    cash: Decimal
    position: Decimal
    max_order_notional: Decimal | None
    max_net_position: Decimal | None
    reserved_cash: Decimal = Decimal(0)       # 在簿 BUY 单冻结资金
    reserved_sell_qty: Decimal = Decimal(0)   # 在簿 SELL 单剩余数量
    buy_leaves_qty: Decimal = Decimal(0)      # 在簿 BUY 单剩余数量


@dataclass
class IdemRecord:
    fingerprint: tuple[Any, ...]
    response: dict


class MatchingEngine:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self.accounts: dict[str, AccountState] = {}
        self.orders: dict[str, OrderState] = {}
        self.trades: list[TradeView] = []
        self.bids: dict[Decimal, deque[str]] = defaultdict(deque)
        self.asks: dict[Decimal, deque[str]] = defaultdict(deque)
        self.submit_idem: dict[str, IdemRecord] = {}
        self.cancel_idem: dict[str, IdemRecord] = {}
        self._last_seq = 0

    # ================= 生命周期 =================
    def replay(self) -> None:
        """依据已持久化事件严格重建状态。乱序/非法事件立即失败（不静默跳过）。"""
        with self._lock:
            expected = 1
            # 幂等重建簿记：submit 的「即时成交」在事件流中紧跟其 ORDER_SUBMITTED
            pending_idem: str | None = None
            pending_order: str | None = None
            submit_keys: dict[str, str] = {}        # idem_key -> order_id
            submit_trades: dict[str, list[str]] = defaultdict(list)
            cancel_keys: dict[str, str] = {}        # idem_key -> order_id
            for event in self.store.replay():
                if event.seq != expected:
                    raise ReplayError(
                        f"事件序号乱序：期望 seq={expected}，实际 seq={event.seq}"
                    )
                self._replay_validate(event)
                self._mutate(event)
                self._last_seq = event.seq

                if event.type is EventType.ORDER_SUBMITTED:
                    key = event.payload.get("idempotency_key")
                    pending_idem = key
                    pending_order = event.payload["order_id"]
                    if key is not None:
                        submit_keys[key] = pending_order
                elif event.type is EventType.TRADE_EXECUTED:
                    if pending_idem is not None and pending_order is not None \
                            and event.payload["taker_order_id"] == pending_order:
                        submit_trades[pending_idem].append(event.payload["trade_id"])
                else:
                    if event.type is EventType.ORDER_CANCELLED:
                        key = event.payload.get("idempotency_key")
                        if key is not None:
                            cancel_keys[key] = event.payload["order_id"]
                    pending_idem = None
                    pending_order = None
                expected += 1

            self._rebuild_idempotency(submit_keys, submit_trades, cancel_keys)
            logger.info("重放完成，共 %d 个事件，订单 %d，成交 %d",
                        self._last_seq, len(self.orders), len(self.trades))

    def _rebuild_idempotency(
        self,
        submit_keys: dict[str, str],
        submit_trades: dict[str, list[str]],
        cancel_keys: dict[str, str],
    ) -> None:
        """从历史事件重建与首次响应一致的幂等结果。"""
        trades_by_id = {t.trade_id: t for t in self.trades}
        for key, order_id in submit_keys.items():
            order = self.orders[order_id]
            tvs = [trades_by_id[tid] for tid in submit_trades.get(key, [])]
            filled = sum((t.quantity for t in tvs), Decimal(0))
            if filled >= order.original_qty:
                status = OrderStatus.FILLED
            elif filled > 0:
                status = OrderStatus.PARTIALLY_FILLED
            else:
                status = OrderStatus.NEW
            self.submit_idem[key] = IdemRecord(
                fingerprint=(
                    order.account_id, order.side.value,
                    str(order.price), str(order.original_qty),
                ),
                response=SubmitResponse(
                    accepted=True,
                    order_id=order_id,
                    idempotency_key=key,
                    status=status,
                    trades=tvs,
                ).model_dump(mode="json"),
            )
        for key, order_id in cancel_keys.items():
            self.cancel_idem[key] = IdemRecord(
                fingerprint=(order_id,),
                response=CancelResponse(
                    cancelled=True, order_id=order_id, idempotency_key=key
                ).model_dump(mode="json"),
            )

    def configure_account(self, config: AccountConfig) -> AccountView:
        with self._lock:
            if config.account_id in self.accounts:
                raise IdempotencyConflictError(
                    f"账户 {config.account_id} 已存在，不允许重复配置"
                )
            payload = AccountConfiguredPayload(
                account_id=config.account_id,
                cash=config.cash,
                position=config.position,
                max_order_notional=config.max_order_notional,
                max_net_position=config.max_net_position,
            ).model_dump(mode="json")
            event = self.store.append(EventType.ACCOUNT_CONFIGURED, payload)
            self._mutate(event)
            self._last_seq = event.seq
            logger.info("配置账户 %s cash=%s position=%s",
                        config.account_id, config.cash, config.position)
            return self.get_account(config.account_id)

    # ================= 提交 =================
    def submit(self, req: SubmitOrderRequest) -> SubmitResponse:
        with self._lock:
            fp = (req.account_id, req.side.value, str(req.price), str(req.quantity))
            if req.idempotency_key is not None:
                rec = self.submit_idem.get(req.idempotency_key)
                if rec is not None:
                    if rec.fingerprint != fp:
                        raise IdempotencyConflictError(
                            f"幂等键 {req.idempotency_key} 已用于不同请求"
                        )
                    logger.info("提交幂等命中 key=%s -> 返回首次结果", req.idempotency_key)
                    # 无论首次是接受还是拒绝，都原样返回（不再次成交/不再次落盘）
                    hit = SubmitResponse.model_validate(rec.response)
                    hit.idempotent_hit = True
                    return hit

            try:
                resp = self._do_submit(req)
            except TradingError as exc:
                # 带幂等键的拒绝也要落盘并记住，保证重复提交返回一致结果
                if req.idempotency_key is not None:
                    rej = OrderRejectedPayload(
                        account_id=req.account_id,
                        side=req.side,
                        price=req.price,
                        quantity=req.quantity,
                        reason=exc.reason,
                        idempotency_key=req.idempotency_key,
                    ).model_dump(mode="json")
                    event = self.store.append(EventType.ORDER_REJECTED, rej)
                    self._mutate(event)
                    self._last_seq = event.seq
                    self.submit_idem[req.idempotency_key] = IdemRecord(
                        fingerprint=fp,
                        response=SubmitResponse(
                            accepted=False,
                            idempotency_key=req.idempotency_key,
                            reject_reason=exc.reason,
                        ).model_dump(mode="json"),
                    )
                logger.info("拒单 acct=%s side=%s price=%s qty=%s reason=%s",
                            req.account_id, req.side.value, req.price,
                            req.quantity, exc.reason.value)
                raise

            if req.idempotency_key is not None:
                self.submit_idem[req.idempotency_key] = IdemRecord(
                    fingerprint=fp, response=resp.model_dump(mode="json")
                )
            return resp

    def _do_submit(self, req: SubmitOrderRequest) -> SubmitResponse:
        if req.price <= 0 or req.quantity <= 0:
            raise TradingError("价格与数量必须为正数", reason=RejectReason.INVALID_REQUEST)
        acct = self.accounts.get(req.account_id)
        if acct is None:
            raise UnknownAccountError(f"未知账户 {req.account_id}")

        notional = req.price * req.quantity
        if acct.max_order_notional is not None and notional > acct.max_order_notional:
            raise OrderNotionalExceededError(
                f"单笔名义金额 {notional} 超过限额 {acct.max_order_notional}"
            )

        if req.side is Side.BUY:
            available_cash = acct.cash - acct.reserved_cash
            if notional > available_cash:
                raise InsufficientCashError(
                    f"买入需冻结 {notional}，可用资金 {available_cash}"
                )
            projected_long = acct.position + acct.buy_leaves_qty + req.quantity
            if acct.max_net_position is not None and projected_long > acct.max_net_position:
                raise NetPositionExceededError(
                    f"买入后预计净持仓 {projected_long} 超过限额 {acct.max_net_position}"
                )
        else:
            available_qty = acct.position - acct.reserved_sell_qty
            if req.quantity > available_qty:
                raise InsufficientPositionError(
                    f"卖出需冻结 {req.quantity}，可用持仓 {available_qty}"
                )
            projected_short = -(acct.position - acct.reserved_sell_qty - req.quantity)
            if acct.max_net_position is not None and projected_short > acct.max_net_position:
                raise NetPositionExceededError(
                    f"卖出后预计净持仓 -{projected_short} 超过限额 {acct.max_net_position}"
                )

        if self._would_self_trade(acct.account_id, req.side, req.price):
            raise SelfTradePreventedError(
                f"账户 {acct.account_id} 在对手方向存在可交叉订单，禁止自成交"
            )

        order_id = self._next_order_id()
        submitted = OrderSubmittedPayload(
            order_id=order_id,
            account_id=req.account_id,
            side=req.side,
            price=req.price,
            original_qty=req.quantity,
            idempotency_key=req.idempotency_key,
        ).model_dump(mode="json")
        event = self.store.append(EventType.ORDER_SUBMITTED, submitted)
        if event.seq != int(order_id[1:]):
            raise ReplayError("内部错误：订单 ID 与事件序号不一致")
        self._mutate(event)
        self._last_seq = event.seq
        logger.info("订单入簿 id=%s acct=%s side=%s price=%s qty=%s",
                    order_id, req.account_id, req.side.value, req.price, req.quantity)

        trades = self._match(order_id)
        order = self.orders[order_id]
        return SubmitResponse(
            accepted=True,
            order_id=order_id,
            idempotency_key=req.idempotency_key,
            status=self._order_view(order).status,
            trades=trades,
        )

    # ================= 撮合 =================
    def _match(self, taker_id: str) -> list[TradeView]:
        taker = self.orders[taker_id]
        trades: list[TradeView] = []
        while taker.leaves_qty > 0:
            maker_id = self._best_opposite(taker.side)
            if maker_id is None:
                break
            maker = self.orders[maker_id]
            if not self._crosses(taker.side, taker.price, maker.price):
                break
            qty = min(taker.leaves_qty, maker.leaves_qty)
            # 成交 ID 取即将写入的事件序号（append 后即为 self._last_seq + 1）
            next_seq = self._last_seq + 1
            trade_id = f"T{next_seq}"
            payload = TradeExecutedPayload(
                trade_id=trade_id,
                maker_order_id=maker.order_id,
                taker_order_id=taker.order_id,
                maker_account_id=maker.account_id,
                taker_account_id=taker.account_id,
                taker_side=taker.side,
                price=maker.price,   # 价格优先：成交价=maker 价
                quantity=qty,
            ).model_dump(mode="json")
            event = self.store.append(EventType.TRADE_EXECUTED, payload)
            if event.seq != next_seq:
                raise ReplayError("内部错误：成交 ID 与事件序号不一致")
            tv = self._mutate(event)
            self._last_seq = event.seq
            trades.append(tv)
            logger.info("成交 %s taker=%s maker=%s price=%s qty=%s",
                        trade_id, taker.order_id, maker.order_id, maker.price, qty)
        return trades

    def _best_opposite(self, taker_side: Side) -> str | None:
        book = self.asks if taker_side is Side.BUY else self.bids
        # 按价格优先遍历价位（BUY 吃最低卖价，SELL 吃最高买价），
        # 队列头部若是已成交/已撤单则惰性弹出；当前价位清空后继续下一价位。
        while book:
            best_price = min(book.keys()) if taker_side is Side.BUY else max(book.keys())
            dq = book[best_price]
            while dq:
                oid = dq[0]
                order = self.orders.get(oid)
                if order is None or order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                    dq.popleft()
                    continue
                return oid
            del book[best_price]
        return None

    @staticmethod
    def _crosses(taker_side: Side, taker_price: Decimal, maker_price: Decimal) -> bool:
        return taker_price >= maker_price if taker_side is Side.BUY else taker_price <= maker_price

    def _would_self_trade(self, account_id: str, side: Side, price: Decimal) -> bool:
        book = self.asks if side is Side.BUY else self.bids
        for mp, dq in book.items():
            if not self._crosses(side, price, mp):
                continue
            for oid in dq:
                order = self.orders.get(oid)
                if order is None or order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED):
                    continue
                if order.account_id == account_id:
                    return True
        return False

    # ================= 撤单 =================
    def cancel(self, order_id: str, idempotency_key: str | None = None) -> CancelResponse:
        with self._lock:
            if idempotency_key is not None:
                rec = self.cancel_idem.get(idempotency_key)
                if rec is not None:
                    if rec.fingerprint != (order_id,):
                        raise IdempotencyConflictError(
                            f"幂等键 {idempotency_key} 已用于撤销其他订单"
                        )
                    logger.info("撤单幂等命中 key=%s -> 返回首次结果", idempotency_key)
                    hit = CancelResponse.model_validate(rec.response)
                    hit.idempotent_hit = True
                    return hit
            try:
                resp = self._do_cancel(order_id, idempotency_key)
            except TradingError as exc:
                if idempotency_key is not None:
                    self.cancel_idem[idempotency_key] = IdemRecord(
                        fingerprint=(order_id,),
                        response=CancelResponse(
                            cancelled=False,
                            order_id=order_id,
                            idempotency_key=idempotency_key,
                            reason=exc.reason,
                        ).model_dump(mode="json"),
                    )
                logger.info("撤单失败 order=%s reason=%s", order_id, exc.reason.value)
                raise
            if idempotency_key is not None:
                self.cancel_idem[idempotency_key] = IdemRecord(
                    fingerprint=(order_id,), response=resp.model_dump(mode="json")
                )
            return resp

    def _do_cancel(self, order_id: str, idempotency_key: str | None) -> CancelResponse:
        order = self.orders.get(order_id)
        if order is None:
            raise UnknownOrderError(f"未知订单 {order_id}")
        if order.status is OrderStatus.CANCELLED:
            raise AlreadyCancelledError(f"订单 {order_id} 已撤销")
        if order.status is OrderStatus.FILLED:
            raise OrderNotActiveError(f"订单 {order_id} 已全部成交，不可撤销")
        payload = OrderCancelledPayload(
            order_id=order_id, idempotency_key=idempotency_key
        ).model_dump(mode="json")
        event = self.store.append(EventType.ORDER_CANCELLED, payload)
        self._mutate(event)
        self._last_seq = event.seq
        logger.info("撤单成功 order=%s released_leaves=%s", order_id, order.leaves_qty)
        return CancelResponse(
            cancelled=True, order_id=order_id, idempotency_key=idempotency_key
        )

    # ================= 查询 =================
    def _order_view(self, order: OrderState) -> OrderView:
        status = order.status
        if status is OrderStatus.NEW and order.filled_qty > 0:
            status = OrderStatus.PARTIALLY_FILLED
        return OrderView(
            order_id=order.order_id,
            account_id=order.account_id,
            side=order.side,
            price=order.price,
            original_qty=order.original_qty,
            filled_qty=order.filled_qty,
            leaves_qty=order.leaves_qty,
            status=status,
        )

    def get_order(self, order_id: str) -> OrderView:
        with self._lock:
            order = self.orders.get(order_id)
            if order is None:
                raise UnknownOrderError(f"未知订单 {order_id}")
            return self._order_view(order)

    def get_account(self, account_id: str) -> AccountView:
        with self._lock:
            a = self.accounts.get(account_id)
            if a is None:
                raise UnknownAccountError(f"未知账户 {account_id}")
            return AccountView(
                account_id=a.account_id,
                cash=a.cash,
                reserved_cash=a.reserved_cash,
                position=a.position,
                reserved_sell_qty=a.reserved_sell_qty,
                max_order_notional=a.max_order_notional,
                max_net_position=a.max_net_position,
            )

    def book_snapshot(self) -> dict:
        with self._lock:
            def levels(book: dict[Decimal, deque[str]]) -> list[dict]:
                out = []
                for price, dq in book.items():
                    qty = sum(
                        (self.orders[o].leaves_qty for o in dq
                         if o in self.orders
                         and self.orders[o].status not in (OrderStatus.FILLED, OrderStatus.CANCELLED)),
                        Decimal(0),
                    )
                    if qty > 0:
                        out.append({"price": str(price), "quantity": str(qty)})
                out.sort(key=lambda x: Decimal(x["price"]), reverse=True)
                return out

            bids = levels(self.bids)
            asks = levels(self.asks)
            asks.sort(key=lambda x: Decimal(x["price"]))
            return {"bids": bids, "asks": asks}

    def list_trades(self) -> list[TradeView]:
        with self._lock:
            return list(self.trades)

    # ================= 内部：ID =================
    def _next_order_id(self) -> str:
        return f"O{self._last_seq + 1}"

    # ================= 内部：重放语义校验 =================
    def _replay_validate(self, event: Event) -> None:
        t = event.type
        try:
            if t is EventType.ACCOUNT_CONFIGURED:
                p = AccountConfiguredPayload.model_validate(event.payload)
                if p.account_id in self.accounts:
                    raise ReplayError(f"seq={event.seq} 账户 {p.account_id} 被重复配置")
            elif t is EventType.ORDER_SUBMITTED:
                p = OrderSubmittedPayload.model_validate(event.payload)
                if p.order_id in self.orders:
                    raise ReplayError(f"seq={event.seq} 订单 {p.order_id} 重复提交")
                if p.account_id not in self.accounts:
                    raise ReplayError(f"seq={event.seq} 提交引用未知账户 {p.account_id}")
            elif t is EventType.ORDER_REJECTED:
                OrderRejectedPayload.model_validate(event.payload)
            elif t is EventType.TRADE_EXECUTED:
                p = TradeExecutedPayload.model_validate(event.payload)
                self._validate_trade(event.seq, p)
            elif t is EventType.ORDER_CANCELLED:
                p = OrderCancelledPayload.model_validate(event.payload)
                if p.order_id not in self.orders:
                    raise ReplayError(f"seq={event.seq} 撤销引用未知订单 {p.order_id}")
                if self.orders[p.order_id].status is OrderStatus.CANCELLED:
                    raise ReplayError(f"seq={event.seq} 订单 {p.order_id} 被重复撤销")
        except ReplayError:
            raise
        except Exception as exc:
            raise ReplayError(f"seq={event.seq} 事件 {t.value} payload 非法：{exc}") from exc

    def _validate_trade(self, seq: int, p: TradeExecutedPayload) -> None:
        maker = self.orders.get(p.maker_order_id)
        taker = self.orders.get(p.taker_order_id)
        if maker is None or taker is None:
            raise ReplayError(f"seq={seq} 成交引用了不存在的订单")
        if maker.order_id == taker.order_id:
            raise ReplayError(f"seq={seq} 成交的买卖双方不能是同一订单")
        if p.quantity <= 0:
            raise ReplayError(f"seq={seq} 成交数量必须为正")
        if p.quantity > maker.leaves_qty or p.quantity > taker.leaves_qty:
            raise ReplayError(f"seq={seq} 成交数量超过订单剩余量")
        if maker.account_id == taker.account_id:
            raise ReplayError(f"seq={seq} 历史成交出现自成交，事件流被污染")
        if maker.side is taker.side:
            raise ReplayError(f"seq={seq} 成交双方方向相同")
        # taker 必须在提交时确实愿意以该价成交；成交价必须等于 maker 挂单价
        if p.price != maker.price:
            raise ReplayError(f"seq={seq} 成交价 {p.price} 不等于 maker 价 {maker.price}")
        if p.taker_side is Side.BUY and taker.price < p.price:
            raise ReplayError(f"seq={seq} BUY taker 限价低于成交价")
        if p.taker_side is Side.SELL and taker.price > p.price:
            raise ReplayError(f"seq={seq} SELL taker 限价高于成交价")
        if p.maker_account_id != maker.account_id or p.taker_account_id != taker.account_id:
            raise ReplayError(f"seq={seq} 成交事件中的账户与订单归属不一致")
        if p.trade_id != f"T{seq}":
            raise ReplayError(f"seq={seq} 成交 ID {p.trade_id} 与序号不一致")

    # ================= 内部：状态突变（唯一入口） =================
    def _mutate(self, event: Event) -> TradeView | None:
        t = event.type
        if t is EventType.ACCOUNT_CONFIGURED:
            self._mutate_account_configured(event.payload)
            return None
        if t is EventType.ORDER_SUBMITTED:
            self._mutate_order_submitted(event.payload)
            return None
        if t is EventType.TRADE_EXECUTED:
            return self._mutate_trade(event.payload)
        if t is EventType.ORDER_CANCELLED:
            self._mutate_order_cancelled(event.payload)
            return None
        if t is EventType.ORDER_REJECTED:
            self._mutate_order_rejected(event.payload)
            return None
        raise ReplayError(f"seq={event.seq} 未知事件类型 {t}")

    def _mutate_account_configured(self, raw: dict) -> None:
        p = AccountConfiguredPayload.model_validate(raw)
        self.accounts[p.account_id] = AccountState(
            account_id=p.account_id,
            cash=p.cash,
            position=p.position,
            max_order_notional=p.max_order_notional,
            max_net_position=p.max_net_position,
        )

    def _mutate_order_submitted(self, raw: dict) -> None:
        p = OrderSubmittedPayload.model_validate(raw)
        order = OrderState(
            order_id=p.order_id,
            account_id=p.account_id,
            side=p.side,
            price=p.price,
            original_qty=p.original_qty,
        )
        self.orders[p.order_id] = order
        acct = self.accounts[p.account_id]
        book = self.bids if p.side is Side.BUY else self.asks
        book[p.price].append(p.order_id)
        if p.side is Side.BUY:
            acct.reserved_cash += p.price * p.original_qty
            acct.buy_leaves_qty += p.original_qty
        else:
            acct.reserved_sell_qty += p.original_qty

    def _mutate_trade(self, raw: dict) -> TradeView:
        p = TradeExecutedPayload.model_validate(raw)
        maker = self.orders[p.maker_order_id]
        taker = self.orders[p.taker_order_id]
        qty = p.quantity
        maker_acct = self.accounts[maker.account_id]
        taker_acct = self.accounts[taker.account_id]

        for o, acct in ((maker, maker_acct), (taker, taker_acct)):
            o.filled_qty += qty
            if o.filled_qty >= o.original_qty:
                o.status = OrderStatus.FILLED

        if p.taker_side is Side.BUY:
            # taker 买入：现金减少，持仓增加；冻结按实际成交价消耗
            taker_acct.reserved_cash -= taker.price * qty
            taker_acct.cash -= p.price * qty
            taker_acct.position += qty
            taker_acct.buy_leaves_qty -= qty
            # maker 卖出：释放冻结数量，现金增加，持仓减少
            maker_acct.reserved_sell_qty -= qty
            maker_acct.cash += p.price * qty
            maker_acct.position -= qty
        else:
            # taker 卖出
            taker_acct.reserved_sell_qty -= qty
            taker_acct.cash += p.price * qty
            taker_acct.position -= qty
            # maker 买入
            maker_acct.reserved_cash -= maker.price * qty
            maker_acct.cash -= p.price * qty
            maker_acct.position += qty
            maker_acct.buy_leaves_qty -= qty

        tv = TradeView(
            trade_id=p.trade_id,
            order_id=p.taker_order_id,
            counter_order_id=p.maker_order_id,
            maker_account_id=p.maker_account_id,
            taker_account_id=p.taker_account_id,
            side=p.taker_side,
            price=p.price,
            quantity=qty,
        )
        self.trades.append(tv)
        return tv

    def _mutate_order_cancelled(self, raw: dict) -> None:
        p = OrderCancelledPayload.model_validate(raw)
        order = self.orders[p.order_id]
        acct = self.accounts[order.account_id]
        leaves = order.leaves_qty
        order.status = OrderStatus.CANCELLED
        if order.side is Side.BUY:
            acct.reserved_cash -= order.price * leaves
            acct.buy_leaves_qty -= leaves
        else:
            acct.reserved_sell_qty -= leaves
        # 从队列移除（惰性清理也可，这里主动移除保证快照准确）
        book = self.bids if order.side is Side.BUY else self.asks
        dq = book.get(order.price)
        if dq is not None and p.order_id in dq:
            dq.remove(p.order_id)
            if not dq:
                del book[order.price]

    def _mutate_order_rejected(self, raw: dict) -> None:
        p = OrderRejectedPayload.model_validate(raw)
        if p.idempotency_key is not None:
            fp = (p.account_id, p.side.value, str(p.price), str(p.quantity))
            self.submit_idem.setdefault(
                p.idempotency_key,
                IdemRecord(
                    fingerprint=fp,
                    response=SubmitResponse(
                        accepted=False,
                        idempotency_key=p.idempotency_key,
                        reject_reason=p.reason,
                    ).model_dump(mode="json"),
                ),
            )
