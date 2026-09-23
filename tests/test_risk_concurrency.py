"""账户级风控、幂等语义与并发竞争测试。

- 单笔名义金额限额、净持仓限额、资金/持仓不足：超限订单不进入撮合。
- 被拒请求不改变订单簿与账户状态。
- 同一幂等键重复提交：一致结果、不重复成交；不同请求复用键 -> 冲突。
- 并发：撤单与成交竞争（单把锁保证要么成交要么撤单，绝不超卖/超买）。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from app.engine import MatchingEngine
from app.errors import IdempotencyConflictError
from app.models import (
    AccountConfig,
    RejectReason,
    Side,
    SubmitOrderRequest,
)
from app.store import EventStore

log = logging.getLogger("test.risk")


@pytest.fixture()
def engine(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.ensure()
    eng = MatchingEngine(store)
    eng.replay()
    eng.configure_account(AccountConfig(
        account_id="A", cash=Decimal("1000"), position=Decimal("0"),
        max_order_notional=Decimal("1000"), max_net_position=Decimal("6")))
    eng.configure_account(AccountConfig(
        account_id="B", cash=Decimal("1000"), position=Decimal("10"),
        max_order_notional=Decimal("2000"), max_net_position=Decimal("10")))
    return eng


def _req(acct, side, price, qty, key=None):
    return SubmitOrderRequest(account_id=acct, side=Side[side],
                              price=Decimal(price), quantity=Decimal(qty),
                              idempotency_key=key)


def test_order_notional_limit(engine):
    # 单独配置名义限额 500 的账户：6*100=600 > 500
    engine.configure_account(AccountConfig(
        account_id="N", cash=Decimal("10000"), position=Decimal("0"),
        max_order_notional=Decimal("500"), max_net_position=Decimal("1000")))
    with pytest.raises(Exception) as ei:
        engine.submit(_req("N", "BUY", "100", "6", "big"))
    log.info("单笔限额判定 reason=%s（期望 RISK_ORDER_NOTIONAL_EXCEEDED）",
             ei.value.reason.value)
    assert ei.value.reason is RejectReason.RISK_ORDER_NOTIONAL_EXCEEDED
    snap = engine.book_snapshot()
    n = engine.get_account("N")
    log.info("拒单后簿 bids=%s，N 冻结资金=%s（期望空 / 0）", snap["bids"], n.reserved_cash)
    assert snap["bids"] == [] and n.reserved_cash == 0


def test_net_position_limit_buy(engine):
    # 净持仓上限 6：买 6 合法，再买任何数量都会使预计净持仓 > 6
    r1 = engine.submit(_req("A", "BUY", "100", "6", "ok6"))
    assert r1.accepted
    # 此时对手无卖单，订单挂在簿里；A 预计净持仓已达上限 6
    with pytest.raises(Exception) as ei:
        engine.submit(_req("A", "BUY", "100", "1", "one-more"))
    log.info("净持仓限额判定 reason=%s（期望 RISK_NET_POSITION_EXCEEDED）",
             ei.value.reason.value)
    assert ei.value.reason is RejectReason.RISK_NET_POSITION_EXCEEDED


def test_insufficient_cash(engine):
    # 先挂买单冻结 600（无对手卖单，进入订单簿），可用现金剩 400
    engine.submit(_req("A", "BUY", "100", "6", "freeze6"))
    with pytest.raises(Exception) as ei:
        engine.submit(_req("A", "BUY", "100", "5", "want5"))  # 还需 500 > 可用 400
    log.info("资金不足判定 reason=%s（期望 INSUFFICIENT_CASH）", ei.value.reason.value)
    assert ei.value.reason is RejectReason.INSUFFICIENT_CASH


def test_insufficient_position_sell(engine):
    with pytest.raises(Exception) as ei:
        engine.submit(_req("B", "SELL", "100", "11", "sell11"))  # 只有 10
    log.info("持仓不足判定 reason=%s（期望 INSUFFICIENT_POSITION）", ei.value.reason.value)
    assert ei.value.reason is RejectReason.INSUFFICIENT_POSITION


def test_rejected_idempotent_returns_consistent(engine):
    engine.configure_account(AccountConfig(
        account_id="N2", cash=Decimal("10000"), position=Decimal("0"),
        max_order_notional=Decimal("500"), max_net_position=Decimal("1000")))
    # 首次超名义限额被拒（带幂等键，落一条 ORDER_REJECTED），重复提交必须一致且不新增事件
    with pytest.raises(Exception):
        engine.submit(_req("N2", "BUY", "100", "6", "rej-key"))
    events_before = engine._last_seq
    resp = engine.submit(_req("N2", "BUY", "100", "6", "rej-key"))
    log.info("拒绝幂等判定 accepted=%s reason=%s seq 不变=%s",
             resp.accepted, resp.reject_reason.value if resp.reject_reason else None,
             engine._last_seq == events_before)
    assert resp.accepted is False
    assert resp.reject_reason is RejectReason.RISK_ORDER_NOTIONAL_EXCEEDED
    assert engine._last_seq == events_before  # 没有新增事件


def test_idempotency_conflict_on_different_request(engine):
    engine.submit(_req("A", "BUY", "100", "2", "dup-key"))
    with pytest.raises(IdempotencyConflictError) as ei:
        engine.submit(_req("A", "BUY", "100", "3", "dup-key"))  # 同键不同量
    log.info("幂等冲突判定 reason=%s", ei.value.reason.value)
    assert ei.value.reason is RejectReason.IDEMPOTENCY_CONFLICT


def test_idempotent_repeat_does_not_double_fill(engine):
    # B 卖 5，A 用同键买 2 两次：只应成交一次 2
    engine.submit(_req("B", "SELL", "100", "5", "b5"))
    r1 = engine.submit(_req("A", "BUY", "100", "2", "a2"))
    r2 = engine.submit(_req("A", "BUY", "100", "2", "a2"))
    log.info("重复提交判定：两次成交数=%d/%d，成交事件总数=%d（期望 1/1/1）",
             len(r1.trades), len(r2.trades), len(engine.list_trades()))
    assert len(r1.trades) == len(r2.trades) == 1
    assert len(engine.list_trades()) == 1
    assert r2.idempotent_hit is True
    d1, d2 = r1.model_dump(), r2.model_dump()
    d2["idempotent_hit"] = d1["idempotent_hit"]
    assert d2 == d1


def test_concurrent_cancel_vs_fill_race(engine):
    """撤单与成交竞争：结果必须二选一，且数量守恒、不超卖。"""
    sell = engine.submit(_req("B", "SELL", "100", "5", "race-sell"))
    outcomes: dict[str, object] = {}

    def do_cancel():
        try:
            outcomes["cancel"] = engine.cancel(sell.order_id).cancelled
        except Exception as exc:  # 已成交则撤销被拒
            outcomes["cancel"] = False
            outcomes["cancel_reason"] = exc.reason

    def do_buy():
        try:
            r = engine.submit(_req("A", "BUY", "100", "5", "race-buy"))
            outcomes["bought"] = sum((t.quantity for t in r.trades), Decimal(0))
        except Exception as exc:
            outcomes["bought"] = Decimal(0)
            outcomes["buy_reason"] = exc.reason

    t1 = threading.Thread(target=do_cancel)
    t2 = threading.Thread(target=do_buy)
    t1.start(); t2.start(); t1.join(); t2.join()

    cancelled = outcomes.get("cancel") is True
    bought = outcomes.get("bought", Decimal(0))
    log.info("并发竞争判定：撤单成功=%s，买入成交量=%s（二者互斥：撤单成功则成交 0）",
             cancelled, bought)
    assert cancelled == (bought == Decimal(0))
    # 守恒：B 的 持仓-冻结 与 A 的持仓变化对应；总持仓恒为 10
    a = engine.get_account("A"); b = engine.get_account("B")
    assert a.position + b.position == Decimal("10")
    # B 的在簿冻结与其订单剩余一致
    remaining = engine.get_order(sell.order_id).leaves_qty \
        if engine.get_order(sell.order_id).status.value not in ("CANCELLED", "FILLED") else Decimal(0)
    assert b.reserved_sell_qty == remaining
    log.info("最终 A.pos=%s B.pos=%s B.reserved=%s 合计持仓=%s",
             a.position, b.position, b.reserved_sell_qty, a.position + b.position)


def test_concurrent_buys_respect_cash_and_position(tmp_path):
    """并发多笔买单：成交总量不得超过卖家挂出量，资金不得为负。"""
    store = EventStore(tmp_path / "events2.jsonl"); store.ensure()
    eng = MatchingEngine(store); eng.replay()
    eng.configure_account(AccountConfig(account_id="M", cash=Decimal("0"),
                                        position=Decimal("100")))
    for i in range(20):
        eng.configure_account(AccountConfig(account_id=f"T{i}", cash=Decimal("1000"),
                                            position=Decimal("0")))
    eng.submit(SubmitOrderRequest(account_id="M", side=Side.SELL,
                                  price=Decimal("10"), quantity=Decimal("100"),
                                  idempotency_key="m-sell"))

    def buy(i):
        try:
            return eng.submit(SubmitOrderRequest(
                account_id=f"T{i}", side=Side.BUY, price=Decimal("10"),
                quantity=Decimal("10"), idempotency_key=f"buy-{i}"))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(buy, range(20)))
    total_filled = sum(
        (sum((t.quantity for t in r.trades), Decimal(0)) for r in results if r),
        Decimal(0))
    log.info("并发买单判定：总成交量=%s（期望恰好 100，不超卖）", total_filled)
    assert total_filled == Decimal("100")
    m = eng.get_account("M")
    assert m.position == Decimal("0")  # 100 全部卖出
