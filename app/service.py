"""交易服务：编排事件日志、订单簿、账户与风控（线程安全）。

关键约定：
- 所有状态变更遵循“先持久化事件、再修改内存”：先 append 完整事件序列
  （ORDER_SUBMITTED 及其全部 FILL），fsync 成功后才更新订单簿与账户，
  因而崩溃恢复不会出现内存超前于日志的幻影成交。
- 被拒绝订单同样记录一条带 reject_reason 的 ORDER_SUBMITTED 事件，
  幂等重放时返回一致结果，且绝不触碰订单簿与账户。
- 单一 RLock 串行化提交/撤单/查询，杜绝并发超限与重复成交。
"""
from __future__ import annotations

import logging
import threading
from decimal import Decimal, InvalidOperation
from typing import Any

from app.eventlog import EventLog
from app.models import (
    Account,
    AccountRiskSnapshot,
    Fill,
    Order,
    OrderStatus,
    RejectReason,
    Side,
    SymbolRiskSnapshot,
    TradingError,
)
from app.orderbook import OrderBook
from app.risk import RiskManager

logger = logging.getLogger("matching.service")

ZERO = Decimal(0)


def D(value: Any) -> Decimal:
    """把 int/str/Decimal 严格转为 Decimal；浮点与非法值直接拒绝。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise InvalidOperation("bool is not a valid decimal")
    if isinstance(value, float):
        # 禁止从二进制浮点构造，避免 0.1 类误差
        raise InvalidOperation("float input is not allowed, use str/int")
    return Decimal(str(value))


class TradingService:
    def __init__(self, event_log: EventLog | None = None,
                 risk: RiskManager | None = None):
        self._log = event_log
        self._risk = risk or RiskManager()
        self._lock = threading.RLock()
        self._seq = 0
        self._accounts: dict[str, Account] = {}
        self._books: dict[str, OrderBook] = {}
        self._orders: dict[str, Order] = {}              # 全部订单（含终结）
        self._active: dict[str, Order] = {}             # 仅在簿有效订单
        self._idempotency: dict[str, str] = {}           # client_order_id -> order_id
        self._fills: list[Fill] = []
        self._replayed = False

    # ============ 基础配置（账户/品种，同样走事件） ============
    def register_symbol(self, symbol: str) -> None:
        with self._lock:
            if symbol in self._books:
                return
            event = self._persist({"type": "SYMBOL_REGISTERED", "symbol": symbol})
            self._books[symbol] = OrderBook(symbol)
            logger.info("symbol registered seq=%s symbol=%s", event["seq"], symbol)

    def open_account(self, account_id: str, cash: Any = ZERO,
                     max_position: dict[str, Any] | None = None,
                     max_order_notional: Any = Decimal("Infinity")) -> Account:
        with self._lock:
            if account_id in self._accounts:
                return self._accounts[account_id]
            cash_d, cap_d = D(cash), D(max_order_notional)
            limits = {s: D(v) for s, v in (max_position or {}).items()}
            event = self._persist({
                "type": "ACCOUNT_OPENED",
                "account_id": account_id,
                "cash": str(cash_d),
                "max_position": {s: str(v) for s, v in limits.items()},
                "max_order_notional": str(cap_d),
            })
            acc = Account(account_id, cash_d, {}, dict(limits), cap_d)
            self._accounts[account_id] = acc
            logger.info("account opened seq=%s id=%s cash=%s", event["seq"],
                        account_id, cash_d)
            return acc

    def deposit(self, account_id: str, symbol: str | None = None,
                quantity: Any = ZERO, cash: Any = ZERO) -> None:
        """入金/入券（测试与种子账户用），同样落事件以支持重放。"""
        with self._lock:
            if account_id not in self._accounts:
                raise TradingError(RejectReason.UNKNOWN_ACCOUNT,
                                   f"account {account_id} not found")
            qty_d, cash_d = D(quantity), D(cash)
            if qty_d < 0 or cash_d < 0:
                raise TradingError(RejectReason.INVALID_QUANTITY,
                                   "deposit amount must be non-negative")
            self._persist({
                "type": "DEPOSIT",
                "account_id": account_id,
                "symbol": symbol,
                "quantity": str(qty_d),
                "cash": str(cash_d),
            })
            acc = self._accounts[account_id]
            acc.cash += cash_d
            if symbol is not None and qty_d != 0:
                acc.positions[symbol] = acc.positions.get(symbol, ZERO) + qty_d
            logger.info("deposit acct=%s symbol=%s qty=%s cash=%s",
                        account_id, symbol, qty_d, cash_d)

    def adjust_limits(self, account_id: str,
                      max_position: dict[str, Any] | None = None,
                      max_order_notional: Any | None = None,
                      reset_positions: bool = False) -> Account:
        """运行中调整账户持仓限额与单笔名义额上限（先持久化、再生效）。

        - ``max_position``：按品种合并覆盖；``reset_positions=True`` 时整体替换，
          未列出的品种限额被移除（即不再限仓）。
        - ``max_order_notional``：仅在传入时覆盖单笔名义额上限。
        收紧限额不强制取消在途订单，只影响后续新单准入；风险快照会标出
        在途订单全部成交后的超限品种与已超过新单笔上限的在途订单。
        """
        with self._lock:
            acc = self._accounts.get(account_id)
            if acc is None:
                raise TradingError(RejectReason.UNKNOWN_ACCOUNT,
                                   f"account {account_id} not found")
            new_positions = (
                {s: D(v) for s, v in (max_position or {}).items()}
                if max_position is not None else None)
            new_notional = D(max_order_notional) \
                if max_order_notional is not None else None
            if new_positions is not None:
                for sym, v in new_positions.items():
                    if v.is_nan() or v < 0:
                        raise TradingError(RejectReason.INVALID_QUANTITY,
                                           f"limit for {sym} must be >= 0")
            if new_notional is not None and (
                    new_notional.is_nan() or new_notional < 0):
                raise TradingError(RejectReason.INVALID_PRICE,
                                   "max_order_notional must be >= 0")

            event = self._persist({
                "type": "LIMITS_ADJUSTED",
                "account_id": account_id,
                "max_position": ({s: str(v) for s, v in new_positions.items()}
                                 if new_positions is not None else None),
                "max_order_notional": (str(new_notional)
                                       if new_notional is not None else None),
                "reset_positions": bool(reset_positions),
            })
            if new_positions is not None:
                acc.max_position = (dict(new_positions) if reset_positions
                                    else {**acc.max_position, **new_positions})
            if new_notional is not None:
                acc.max_order_notional = new_notional
            logger.info(
                "limits adjusted seq=%s acct=%s positions=%s notional=%s",
                event["seq"], account_id,
                {s: str(v) for s, v in acc.max_position.items()},
                acc.max_order_notional)
            return acc

    # ============ 下单 ============
    def submit_order(self, client_order_id: str, symbol: str, side: str | Side,
                     price: Any, quantity: Any, account: str) -> Order:
        side = Side(side)
        with self._lock:
            return self._submit_locked(client_order_id, symbol, side,
                                       D(price), D(quantity), account)

    def _submit_locked(self, client_order_id: str, symbol: str, side: Side,
                       price: Decimal, quantity: Decimal, account_id: str) -> Order:
        logger.info("submit input coid=%s symbol=%s side=%s price=%s qty=%s acct=%s",
                    client_order_id, symbol, side.value, price, quantity, account_id)

        # --- 幂等：重复提交返回同一结果，绝不二次成交 ---
        existing_id = self._idempotency.get(client_order_id)
        if existing_id is not None:
            existing = self._orders[existing_id]
            same_payload = (existing.symbol == symbol and existing.side == side
                            and existing.price == price
                            and existing.quantity == quantity
                            and existing.account == account_id)
            if same_payload:
                logger.info("idempotent hit coid=%s -> order=%s status=%s",
                            client_order_id, existing_id, existing.status.value)
                return existing
            raise TradingError(
                RejectReason.DUPLICATE_IDEMPOTENCY_KEY,
                f"client_order_id {client_order_id} already bound to "
                f"order {existing_id} with different parameters",
            )

        # --- 静态校验（无副作用） ---
        if account_id not in self._accounts:
            raise TradingError(RejectReason.UNKNOWN_ACCOUNT,
                               f"account {account_id} not found")
        if symbol not in self._books:
            raise TradingError(RejectReason.UNKNOWN_SYMBOL,
                               f"symbol {symbol} not registered")
        if not (price.is_finite() and price > 0):
            raise TradingError(RejectReason.INVALID_PRICE, f"price={price}")
        if not (quantity.is_finite() and quantity > 0):
            raise TradingError(RejectReason.INVALID_QUANTITY, f"qty={quantity}")

        acc = self._accounts[account_id]
        # 正式 order_id 取自 ORDER_SUBMITTED 事件序号，故先用临时 id 完成预检
        order = Order(order_id="", client_order_id=client_order_id,
                      symbol=symbol, side=side, price=price, quantity=quantity,
                      account=account_id)

        # --- 风控 + 自成交预检（plan 不修改订单簿，拒绝零副作用） ---
        reject_reason = self._precheck_locked(acc, order)
        if reject_reason is not None:
            return self._reject_locked(order, reject_reason)

        plan = self._books[symbol].plan_match(order)
        for resting, _qty in plan:
            try:
                self._risk.check_self_trade(order, resting)
            except TradingError as exc:
                return self._reject_locked(order, exc.reason)

        # --- 先持久化：提交事件（正式 order_id 由事件序号决定） ---
        submit_event = self._persist({
            "type": "ORDER_SUBMITTED",
            "reject_reason": None,
            "order": {
                "order_id": "",
                "client_order_id": client_order_id,
                "symbol": symbol, "side": side.value,
                "price": str(price), "quantity": str(quantity),
                "account": account_id,
            },
        })
        order.order_id = f"O{submit_event['seq']}"
        order.sequence = submit_event["seq"]
        fill_events: list[tuple[dict, Order, Decimal]] = []
        for resting, qty in plan:
            buy, sell = (order, resting) if side == Side.BUY else (resting, order)
            ev = self._persist({
                "type": "FILL",
                "fill": {
                    "fill_id": "",   # persist 后由事件序号决定
                    "symbol": symbol,
                    "price": str(resting.price),
                    "quantity": str(qty),
                    "buy_order_id": buy.order_id,
                    "sell_order_id": sell.order_id,
                    "buy_account": buy.account,
                    "sell_account": sell.account,
                    "taker_order_id": order.order_id,
                },
            })
            fill_events.append((ev, resting, qty))

        # --- 日志落盘成功后再改内存 ---
        self._orders[order.order_id] = order
        self._idempotency[client_order_id] = order.order_id
        self._books[symbol].commit(order, plan)
        if order.is_active:
            self._active[order.order_id] = order
        for resting, _q in plan:
            if not resting.is_active:
                self._active.pop(resting.order_id, None)
        for ev, resting, qty in fill_events:
            self._apply_fill_to_accounts(ev["fill"])
            fill = self._fill_from_event(ev)
            self._fills.append(fill)
            logger.info(
                "fill seq=%s %s qty=%s@%s buy=%s sell=%s taker=%s",
                fill.sequence, symbol, qty, resting.price,
                fill.buy_order_id, fill.sell_order_id, fill.taker_order_id)
        logger.info("submit accepted order=%s status=%s filled=%s remaining=%s",
                    order.order_id, order.status.value, order.filled_qty,
                    order.remaining)
        return order

    def _precheck_locked(self, acc: Account, order: Order) -> RejectReason | None:
        reserved_sell_qty = ZERO
        reserved_buy_notional = ZERO
        resting_buy_qty = ZERO
        for active in self._active.values():
            if active.account != acc.account_id:
                continue
            if active.side == Side.SELL:
                if active.symbol == order.symbol:
                    reserved_sell_qty += active.remaining
            else:
                # 跨品种共享现金：所有品种在途买单名义额一起占用
                reserved_buy_notional += active.remaining * active.price
                if active.symbol == order.symbol:
                    resting_buy_qty += active.remaining
        try:
            self._risk.check_order(
                acc, order, reserved_sell_qty,
                reserved_buy_notional, resting_buy_qty)
        except TradingError as exc:
            logger.info("risk reject order-coid=%s reason=%s",
                        order.client_order_id, exc.reason.value)
            return exc.reason
        return None

    def _reject_locked(self, order: Order,
                       reason: RejectReason) -> Order:
        """拒绝也要落事件，保证幂等重放返回一致结果；不触碰订单簿与账户。"""
        event = self._persist({
            "type": "ORDER_SUBMITTED",
            "reject_reason": reason.value,
            "order": self._order_payload(order),
        })
        order.order_id = f"O{event['seq']}"
        order.sequence = event["seq"]
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self._orders[order.order_id] = order
        self._idempotency[order.client_order_id] = order.order_id
        logger.info("submit rejected order=%s coid=%s reason=%s",
                    order.order_id, order.client_order_id, reason.value)
        return order

    # ============ 撤单 ============
    def cancel_order(self, order_id: str) -> Order:
        with self._lock:
            logger.info("cancel input order_id=%s", order_id)
            order = self._orders.get(order_id)
            if order is None:
                raise TradingError(RejectReason.ORDER_NOT_FOUND,
                                   f"order {order_id} not found")
            if order.status == OrderStatus.FILLED:
                raise TradingError(RejectReason.ORDER_ALREADY_FILLED,
                                   f"order {order_id} already fully filled")
            if order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                raise TradingError(RejectReason.ORDER_ALREADY_CANCELLED,
                                   f"order {order_id} status={order.status.value}")
            # 先落盘后撤单
            self._persist({"type": "ORDER_CANCELLED", "order_id": order_id})
            try:
                self._books[order.symbol].remove(order_id)
            except KeyError as exc:
                raise TradingError(RejectReason.CORRUPT_EVENT,
                                   f"active order {order_id} missing in book") from exc
            self._active.pop(order_id, None)
            logger.info("cancel accepted order=%s symbol=%s released_qty=%s",
                        order_id, order.symbol, order.remaining)
            return order

    # ============ 查询 ============
    def get_order(self, order_id: str) -> Order:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise TradingError(RejectReason.ORDER_NOT_FOUND,
                                   f"order {order_id} not found")
            return order

    def get_fills(self, symbol: str | None = None) -> list[Fill]:
        with self._lock:
            return [f for f in self._fills if symbol is None or f.symbol == symbol]

    def book_snapshot(self, symbol: str) -> dict:
        with self._lock:
            if symbol not in self._books:
                raise TradingError(RejectReason.UNKNOWN_SYMBOL,
                                   f"symbol {symbol} not registered")
            return self._books[symbol].snapshot()

    def get_account(self, account_id: str) -> Account:
        with self._lock:
            acc = self._accounts.get(account_id)
            if acc is None:
                raise TradingError(RejectReason.UNKNOWN_ACCOUNT,
                                   f"account {account_id} not found")
            return acc

    def risk_snapshot(self, account_id: str) -> AccountRiskSnapshot:
        """返回组合级风险快照。

        全部汇总在与下单/撤单/成交相同的 RLock 内基于最终态完成，
        快照中各字段严格对应同一串行化时点，绝不呈现中间态或重复占用。
        """
        with self._lock:
            acc = self._accounts.get(account_id)
            if acc is None:
                raise TradingError(RejectReason.UNKNOWN_ACCOUNT,
                                   f"account {account_id} not found")

            reserved_buy_notional = ZERO
            projected_cash = acc.cash
            per_sell: dict[str, Decimal] = {}
            per_buy_qty: dict[str, Decimal] = {}
            over_capped: list[str] = []
            for active in self._active.values():
                if active.account != account_id:
                    continue
                notional = active.remaining * active.price
                if active.side == Side.BUY:
                    reserved_buy_notional += notional
                    per_buy_qty[active.symbol] = (
                        per_buy_qty.get(active.symbol, ZERO) + active.remaining)
                    projected_cash -= notional
                else:
                    per_sell[active.symbol] = (
                        per_sell.get(active.symbol, ZERO) + active.remaining)
                    projected_cash += notional
                if notional > acc.max_order_notional:
                    over_capped.append(active.order_id)

            symbols: dict[str, SymbolRiskSnapshot] = {}
            all_symbols = (set(acc.positions) | set(acc.max_position)
                           | set(per_sell) | set(per_buy_qty))
            for symbol in sorted(all_symbols):
                position = acc.positions.get(symbol, ZERO)
                reserved_sell = per_sell.get(symbol, ZERO)
                resting_buy = per_buy_qty.get(symbol, ZERO)
                projected_position = position + resting_buy - reserved_sell
                limit = acc.max_position.get(symbol)
                symbols[symbol] = SymbolRiskSnapshot(
                    symbol=symbol,
                    position=position,
                    reserved_sell=reserved_sell,
                    available_position=position - reserved_sell,
                    resting_buy_qty=resting_buy,
                    projected_position=projected_position,
                    max_position=limit,
                    limit_breached=(limit is not None
                                    and abs(position) > limit),
                    projected_limit_breached=(limit is not None
                                              and abs(projected_position) > limit),
                )

            current_breached = any(s.limit_breached for s in symbols.values())
            projected_breached = any(
                s.projected_limit_breached for s in symbols.values())
            return AccountRiskSnapshot(
                account_id=account_id,
                cash=acc.cash,
                reserved_buy_notional=reserved_buy_notional,
                available_cash=acc.cash - reserved_buy_notional,
                max_order_notional=acc.max_order_notional,
                projected_cash=projected_cash,
                cash_breached=acc.cash < 0,
                projected_cash_breached=projected_cash < 0,
                current_limit_breached=current_breached,
                projected_limit_breached=projected_breached,
                over_capped_orders=sorted(over_capped),
                symbols=symbols,
            )

    def list_symbols(self) -> list[str]:
        with self._lock:
            return list(self._books.keys())

    # ============ 事件工具 ============
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _persist(self, event: dict[str, Any]) -> dict[str, Any]:
        seq = self._next_seq()
        # 确定性 ID：订单/成交 id 直接由事件序号派生，保证重放可复现
        if event.get("type") in ("ORDER_SUBMITTED",) and "order" in event:
            event["order"]["order_id"] = f"O{seq}"
        if event.get("type") == "FILL" and "fill" in event:
            event["fill"]["fill_id"] = f"F{seq}"
        event = {"seq": seq, **event}
        if self._log is not None:
            self._log.append(event)
        return event

    @staticmethod
    def _order_payload(order: Order) -> dict[str, Any]:
        return {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "price": str(order.price),
            "quantity": str(order.quantity),
            "account": order.account,
        }

    def _apply_fill_to_accounts(self, payload: dict[str, Any]) -> None:
        qty, price = D(payload["quantity"]), D(payload["price"])
        buyer = self._accounts[payload["buy_account"]]
        seller = self._accounts[payload["sell_account"]]
        buyer.cash -= qty * price
        buyer.positions[payload["symbol"]] = (
            buyer.positions.get(payload["symbol"], ZERO) + qty)
        seller.cash += qty * price
        seller.positions[payload["symbol"]] = (
            seller.positions.get(payload["symbol"], ZERO) - qty)

    @staticmethod
    def _fill_from_event(event: dict[str, Any]) -> Fill:
        p = event["fill"]
        return Fill(
            fill_id=p.get("fill_id") or f"F{event['seq']}",
            sequence=event["seq"], symbol=p["symbol"],
            price=D(p["price"]), quantity=D(p["quantity"]),
            buy_order_id=p["buy_order_id"], sell_order_id=p["sell_order_id"],
            buy_account=p["buy_account"], sell_account=p["sell_account"],
            taker_order_id=p["taker_order_id"],
        )

    # ============ 重放恢复 ============
    def replay(self, events: list[dict] | None = None) -> None:
        """从事件日志（或给定事件列表）重建全部状态。

        严格校验，任何不一致都抛出异常而不是静默跳过：
        - seq 必须从 1 开始严格 +1（乱序/缺口/重复 => OUT_OF_ORDER_EVENT）；
        - 订单/成交 id 必须与 seq 派生值一致；FILL 引用必须存在、方向自洽、
          数量不超剩余，且买卖方都必须仍在订单簿中（CORRUPT_EVENT）；
        - CANCEL 引用必须是当时仍有效的订单。
        重放结束后校验资金/持仓/成交量守恒。
        """
        with self._lock:
            if events is None:
                if self._log is None:
                    raise TradingError(RejectReason.CORRUPT_EVENT,
                                       "no event log configured")
                events = list(self._log.read_all())

            logger.info("replay start events=%d", len(events))
            # 记账：订单提交事件序号集合 / 各订单已重放成交量
            submitted: set[str] = set()
            submit_seq: dict[str, int] = {}
            filled_qty: dict[str, Decimal] = {}
            deposits_qty: dict[str, Decimal] = {}
            deposits_cash: Decimal = ZERO

            for i, event in enumerate(events, start=1):
                seq = event.get("seq")
                if not isinstance(seq, int) or isinstance(seq, bool) or seq != i:
                    raise TradingError(
                        RejectReason.OUT_OF_ORDER_EVENT,
                        f"expected seq={i}, got seq={seq!r}",
                    )
                self._seq = seq
                etype = event.get("type")
                if etype == "SYMBOL_REGISTERED":
                    symbol = self._require_str(event, "symbol")
                    if symbol in self._books:
                        raise TradingError(
                            RejectReason.CORRUPT_EVENT,
                            f"seq={seq} duplicate symbol {symbol}")
                    self._books[symbol] = OrderBook(symbol)
                elif etype == "ACCOUNT_OPENED":
                    self._replay_account(event)
                elif etype == "DEPOSIT":
                    deposits_cash += self._replay_deposit(event, deposits_qty)
                elif etype == "LIMITS_ADJUSTED":
                    self._replay_limits(event)
                elif etype == "ORDER_SUBMITTED":
                    self._replay_order(event, submitted, submit_seq, filled_qty)
                elif etype == "FILL":
                    self._replay_fill(event, submitted, submit_seq, filled_qty)
                elif etype == "ORDER_CANCELLED":
                    self._replay_cancel(event, submitted, submit_seq)
                else:
                    raise TradingError(
                        RejectReason.CORRUPT_EVENT,
                        f"seq={seq} unknown event type {etype!r}")

            self._replayed = True
            self._verify_conservation(events, deposits_qty, deposits_cash)
            logger.info("replay done seq=%d orders=%d fills=%d",
                        self._seq, len(self._orders), len(self._fills))

    @staticmethod
    def _require_str(event: dict, key: str) -> str:
        value = event.get(key)
        if not isinstance(value, str) or not value:
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"seq={event.get('seq')} field {key} must be non-empty string")
        return value

    def _replay_account(self, event: dict) -> None:
        seq = event["seq"]
        account_id = self._require_str(event, "account_id")
        if account_id in self._accounts:
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} duplicate account {account_id}")
        try:
            cash = D(event["cash"])
            cap = D(event["max_order_notional"])
            limits = {s: D(v) for s, v in event["max_position"].items()}
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"seq={seq} bad account payload: {exc}") from exc
        self._accounts[account_id] = Account(account_id, cash, {}, limits, cap)

    def _replay_deposit(self, event: dict,
                        deposits_qty: dict[str, Decimal]) -> Decimal:
        seq = event["seq"]
        account_id = self._require_str(event, "account_id")
        if account_id not in self._accounts:
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} deposit to unknown account")
        try:
            qty, cash = D(event["quantity"]), D(event["cash"])
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} bad deposit payload: {exc}") from exc
        if qty < 0 or cash < 0:
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} negative deposit")
        symbol = event.get("symbol")
        if symbol is not None and not isinstance(symbol, str):
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} bad deposit symbol")
        acc = self._accounts[account_id]
        acc.cash += cash
        if symbol is not None and qty != 0:
            acc.positions[symbol] = acc.positions.get(symbol, ZERO) + qty
            deposits_qty[symbol] = deposits_qty.get(symbol, ZERO) + qty
        return cash

    def _replay_limits(self, event: dict) -> None:
        seq = event["seq"]
        account_id = self._require_str(event, "account_id")
        if account_id not in self._accounts:
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} limits on unknown account")
        raw_positions = event.get("max_position")
        raw_notional = event.get("max_order_notional")
        reset = event.get("reset_positions", False)
        if not isinstance(reset, bool):
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} reset_positions must be bool")
        try:
            new_positions = (
                {s: D(v) for s, v in raw_positions.items()}
                if raw_positions is not None else None)
            new_notional = D(raw_notional) if raw_notional is not None else None
        except (AttributeError, InvalidOperation, TypeError) as exc:
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"seq={seq} bad limits payload: {exc}") from exc
        acc = self._accounts[account_id]
        if new_positions is not None:
            for sym, v in new_positions.items():
                if not isinstance(sym, str) or v.is_nan() or v < 0:
                    raise TradingError(
                        RejectReason.CORRUPT_EVENT,
                        f"seq={seq} bad position limit {sym}={v}")
            acc.max_position = (dict(new_positions) if reset
                                else {**acc.max_position, **new_positions})
        if new_notional is not None:
            if new_notional.is_nan() or new_notional < 0:
                raise TradingError(RejectReason.CORRUPT_EVENT,
                                   f"seq={seq} bad notional limit")
            acc.max_order_notional = new_notional
        logger.info("replay limits seq=%s acct=%s", seq, account_id)

    def _replay_order(self, event: dict, submitted: set[str],
                      submit_seq: dict[str, int],
                      filled_qty: dict[str, Decimal]) -> None:
        seq = event["seq"]
        p = event.get("order")
        reason = event.get("reject_reason")
        if not isinstance(p, dict):
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} missing order payload")
        try:
            order_id = p["order_id"]
            if order_id != f"O{seq}":
                raise TradingError(
                    RejectReason.CORRUPT_EVENT,
                    f"seq={seq} order_id {order_id!r} != O{seq}")
            coid = p["client_order_id"]
            symbol = p["symbol"]
            side = Side(p["side"])
            price, quantity = D(p["price"]), D(p["quantity"])
            account = p["account"]
        except (KeyError, ValueError, InvalidOperation, TypeError) as exc:
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"seq={seq} bad order payload: {exc}") from exc
        if order_id in submitted:
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} duplicate order {order_id}")
        if symbol not in self._books:
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} order on unknown symbol {symbol}")
        if account not in self._accounts:
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} order on unknown account {account}")
        if not (price.is_finite() and price > 0 and quantity.is_finite()
                and quantity > 0):
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} invalid price/quantity")

        order = Order(order_id, coid, symbol, side, price, quantity, account,
                      sequence=seq)
        submitted.add(order_id)
        submit_seq[order_id] = seq
        filled_qty[order_id] = ZERO
        self._orders[order_id] = order
        if coid in self._idempotency:
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"seq={seq} duplicate client_order_id {coid}")
        self._idempotency[coid] = order_id

        if reason is None:
            # 被接受的订单：全量入簿，后续 FILL 事件再扣减
            self._books[symbol].place(order)
            self._active[order_id] = order
            logger.info("replay order seq=%s id=%s %s %s@%s",
                        seq, order_id, side.value, quantity, price)
        else:
            try:
                rj = RejectReason(reason)
            except ValueError as exc:
                raise TradingError(
                    RejectReason.CORRUPT_EVENT,
                    f"seq={seq} unknown reject_reason {reason!r}") from exc
            order.status = OrderStatus.REJECTED
            order.reject_reason = rj
            logger.info("replay rejected order seq=%s id=%s reason=%s",
                        seq, order_id, rj.value)

    def _replay_fill(self, event: dict, submitted: set[str],
                     submit_seq: dict[str, int],
                     filled_qty: dict[str, Decimal]) -> None:
        seq = event["seq"]
        p = event.get("fill")
        if not isinstance(p, dict):
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} missing fill payload")
        try:
            if p.get("fill_id") != f"F{seq}":
                raise TradingError(
                    RejectReason.CORRUPT_EVENT,
                    f"seq={seq} fill_id {p.get('fill_id')!r} != F{seq}")
            bid, sid = p["buy_order_id"], p["sell_order_id"]
            qty, price = D(p["quantity"]), D(p["price"])
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} bad fill payload: {exc}") from exc
        if bid == sid:
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} self fill on {bid}")
        buy, sell = self._orders.get(bid), self._orders.get(sid)
        if buy is None or sell is None:
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"seq={seq} fill references unknown order(s) {bid}/{sid}")
        if buy.status == OrderStatus.REJECTED or sell.status == OrderStatus.REJECTED:
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"seq={seq} fill involves rejected order")
        if buy.side != Side.BUY or sell.side != Side.SELL:
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"seq={seq} buy/sell order sides inconsistent")
        # 成交时两笔订单都必须尚未被取消（取消后不再成交）
        # 时序：成交事件必须严格晚于买卖双方订单的提交事件
        if bid not in submit_seq or sid not in submit_seq:
            raise TradingError(
                RejectReason.OUT_OF_ORDER_EVENT,
                f"seq={seq} fill references order not yet submitted")
        if submit_seq[bid] >= seq or submit_seq[sid] >= seq:
            raise TradingError(
                RejectReason.OUT_OF_ORDER_EVENT,
                f"seq={seq} fill precedes order submission "
                f"(order seqs {submit_seq[bid]}/{submit_seq[sid]})")
        if not self._books[buy.symbol].get(bid) or \
                not self._books[sell.symbol].get(sid):
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"seq={seq} order {bid}/{sid} not active in book at fill time")
        # 价格必须同时满足买卖双方限价，且等于卖一挂单价记录
        if not (price <= buy.price and price >= sell.price):
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"seq={seq} fill price {price} violates limit prices")
        if not (qty.is_finite() and qty > 0):
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} bad fill quantity")
        for oid, order in ((bid, buy), (sid, sell)):
            done = filled_qty[oid] + qty
            if done > order.quantity:
                raise TradingError(
                    RejectReason.CORRUPT_EVENT,
                    f"seq={seq} cumulative fills {done} exceed order "
                    f"{oid} quantity {order.quantity}")
            filled_qty[oid] = done

        book = self._books[buy.symbol]
        book.apply_fill(bid, qty)
        book.apply_fill(sid, qty)
        for oid in (bid, sid):
            if not book.get(oid):
                self._active.pop(oid, None)
        payload = dict(p)
        payload["symbol"] = buy.symbol
        payload["buy_account"] = buy.account
        payload["sell_account"] = sell.account
        self._apply_fill_to_accounts(payload)
        self._fills.append(self._fill_from_event(event))
        logger.info("replay fill seq=%s %s qty=%s@%s %s<->%s",
                    seq, buy.symbol, qty, price, bid, sid)

    def _replay_cancel(self, event: dict, submitted: set[str],
                       submit_seq: dict[str, int]) -> None:
        seq = event["seq"]
        oid = event.get("order_id")
        if not isinstance(oid, str) or oid not in submit_seq:
            raise TradingError(RejectReason.OUT_OF_ORDER_EVENT,
                               f"seq={seq} cancel references unknown/not-yet-"
                               f"submitted order {oid!r}")
        if submit_seq[oid] >= seq:
            raise TradingError(RejectReason.OUT_OF_ORDER_EVENT,
                               f"seq={seq} cancel precedes submission of {oid}")
        order = self._orders.get(oid)
        if order is None:
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               f"seq={seq} cancel unknown order {oid!r}")
        if not order.is_active or self._books[order.symbol].get(oid) is None:
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"seq={seq} cancel order {oid} not active in book")
        self._books[order.symbol].remove(oid)
        self._active.pop(oid, None)
        logger.info("replay cancel seq=%s order=%s", seq, oid)

    def _verify_conservation(self, events: list[dict],
                             deposits_qty: dict[str, Decimal],
                             deposits_cash: Decimal) -> None:
        # 1) 成交序列与事件流一致
        fill_events = [e for e in events if e["type"] == "FILL"]
        if len(fill_events) != len(self._fills):
            raise TradingError(RejectReason.CORRUPT_EVENT,
                               "fill count mismatch after replay")
        for ev, fill in zip(fill_events, self._fills):
            if fill.sequence != ev["seq"]:
                raise TradingError(RejectReason.CORRUPT_EVENT,
                                   "fill sequence mismatch after replay")
        # 2) 每个订单 filled_qty == 成交事件汇总
        cumulative: dict[str, Decimal] = {}
        for fill in self._fills:
            cumulative[fill.buy_order_id] = (
                cumulative.get(fill.buy_order_id, ZERO) + fill.quantity)
            cumulative[fill.sell_order_id] = (
                cumulative.get(fill.sell_order_id, ZERO) + fill.quantity)
        for oid, order in self._orders.items():
            q = cumulative.get(oid, ZERO)
            if order.filled_qty != q:
                raise TradingError(
                    RejectReason.CORRUPT_EVENT,
                    f"order {oid} filled_qty {order.filled_qty} != fills {q}")
            if order.status == OrderStatus.REJECTED and q != 0:
                raise TradingError(
                    RejectReason.CORRUPT_EVENT,
                    f"rejected order {oid} has fills")
        # 3) 资金守恒：所有账户现金之和不变（成交只是划转）
        cash_total = sum((a.cash for a in self._accounts.values()), ZERO)
        init_cash = ZERO
        for ev in events:
            if ev["type"] == "ACCOUNT_OPENED":
                init_cash += D(ev["cash"])
        if cash_total != init_cash + deposits_cash:
            raise TradingError(
                RejectReason.CORRUPT_EVENT,
                f"cash not conserved: {init_cash} + {deposits_cash} "
                f"-> {cash_total}")
        # 4) 每个品种全市场净持仓 = 累计入券量（成交只在账户间划转）
        net: dict[str, Decimal] = {}
        for acc in self._accounts.values():
            for sym, qty in acc.positions.items():
                net[sym] = net.get(sym, ZERO) + qty
        symbols = set(net) | set(deposits_qty)
        for sym in symbols:
            if net.get(sym, ZERO) != deposits_qty.get(sym, ZERO):
                raise TradingError(
                    RejectReason.CORRUPT_EVENT,
                    f"symbol {sym} net position {net.get(sym, ZERO)} != "
                    f"deposited {deposits_qty.get(sym, ZERO)}")
