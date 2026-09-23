"""撮合规则测试：价格优先、时间优先、部分成交、自成交拒绝、重复撤单、数量守恒。

每个用例都通过 logging 打印输入、关键状态与判定依据（可用 -o log_cli=true 查看）。
"""
from __future__ import annotations

import logging

import pytest
from decimal import Decimal

from app.engine import MatchingEngine
from app.errors import (
    AlreadyCancelledError,
    OrderNotActiveError,
    SelfTradePreventedError,
    UnknownOrderError,
)
from app.models import AccountConfig, RejectReason, Side, SubmitOrderRequest
from app.store import EventStore

log = logging.getLogger("test.matching")


@pytest.fixture()
def engine(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.ensure()
    eng = MatchingEngine(store)
    eng.replay()
    # 三个账户：A 现金多，B/C 有持仓
    eng.configure_account(AccountConfig(
        account_id="A", cash=Decimal("100000"), position=Decimal("2000"),
        max_order_notional=Decimal("100000"), max_net_position=Decimal("100000")))
    eng.configure_account(AccountConfig(
        account_id="B", cash=Decimal("100000"), position=Decimal("2000"),
        max_order_notional=Decimal("100000"), max_net_position=Decimal("100000")))
    eng.configure_account(AccountConfig(
        account_id="C", cash=Decimal("100000"), position=Decimal("2000"),
        max_order_notional=Decimal("100000"), max_net_position=Decimal("100000")))
    return eng


def _sell(eng, acct, price, qty, key=None):
    log.info("输入 SELL acct=%s price=%s qty=%s key=%s", acct, price, qty, key)
    return eng.submit(SubmitOrderRequest(
        account_id=acct, side=Side.SELL, price=Decimal(price),
        quantity=Decimal(qty), idempotency_key=key))


def _buy(eng, acct, price, qty, key=None):
    log.info("输入 BUY  acct=%s price=%s qty=%s key=%s", acct, price, qty, key)
    return eng.submit(SubmitOrderRequest(
        account_id=acct, side=Side.BUY, price=Decimal(price),
        quantity=Decimal(qty), idempotency_key=key))


def test_full_and_partial_fill(engine):
    s1 = _sell(engine, "B", "100", "10", "b-sell")
    assert s1.status.value == "NEW"
    # 部分成交 4
    r = _buy(engine, "A", "100", "4", "a-buy-4")
    log.info("判定：成交笔数=%d，期望=1；taker 状态=%s，期望=FILLED",
             len(r.trades), r.status.value)
    assert len(r.trades) == 1
    assert r.trades[0].quantity == Decimal("4")
    resting = engine.get_order(s1.order_id)
    log.info("maker 剩余：filled=%s leaves=%s status=%s（期望 4/6/PARTIALLY_FILLED）",
             resting.filled_qty, resting.leaves_qty, resting.status.value)
    assert resting.filled_qty == Decimal("4")
    assert resting.leaves_qty == Decimal("6")
    assert resting.status.value == "PARTIALLY_FILLED"
    # 再吃掉剩余 6
    r2 = _buy(engine, "A", "100", "6", "a-buy-rest")
    assert len(r2.trades) == 1
    assert engine.get_order(s1.order_id).status.value == "FILLED"
    # 数量守恒：A 持仓 8，B 持仓 1000-8
    a = engine.get_account("A"); b = engine.get_account("B")
    log.info("守恒判定 A.pos=%s B.pos=%s 合计=%s（期望 1000）",
             a.position, b.position, a.position + b.position)
    assert a.position == Decimal("2010")
    assert a.position + b.position == Decimal("4000")


def test_price_priority_buy_takes_lowest_ask_first(engine):
    s_low = _sell(engine, "B", "99", "5", "s-low")
    s_high = _sell(engine, "C", "101", "5", "s-high")
    r = _buy(engine, "A", "101", "8", "buy-cross")
    # 应先以 99 成交 5，再以 101 成交 3
    prices = [(str(t.price), str(t.quantity)) for t in r.trades]
    log.info("价格优先判定：成交序列=%s（期望先 99x5 再 101x3）", prices)
    assert prices == [("99", "5"), ("101", "3")]
    assert engine.get_order(s_low.order_id).status.value == "FILLED"
    high = engine.get_order(s_high.order_id)
    assert high.leaves_qty == Decimal("2")
    # taker 两笔分别按 maker 价成交，资金守恒：A 现金 = 100000 - (99*5 + 101*3)
    a = engine.get_account("A")
    spent = Decimal("99") * 5 + Decimal("101") * 3
    log.info("资金守恒：A.cash=%s（期望 %s）", a.cash, Decimal("100000") - spent)
    assert a.cash == Decimal("100000") - spent
    assert sum((acc.cash for acc in (engine.get_account("A"),
                                     engine.get_account("B"),
                                     engine.get_account("C"))), Decimal(0)) == Decimal("300000")


def test_time_priority_same_price_fifo(engine):
    s1 = _sell(engine, "B", "100", "3", "t1")
    s2 = _sell(engine, "C", "100", "3", "t2")
    r = _buy(engine, "A", "100", "4", "taker")
    counter = [t.counter_order_id for t in r.trades]
    log.info("时间优先判定：先吃 %s 再吃 %s（期望先 %s 后 %s）",
             counter[0], counter[-1], s1.order_id, s2.order_id)
    assert counter[0] == s1.order_id
    assert engine.get_order(s1.order_id).status.value == "FILLED"
    assert engine.get_order(s2.order_id).leaves_qty == Decimal("2")


def test_no_cross_means_resting(engine):
    _sell(engine, "B", "110", "5", "ask110")
    r = _buy(engine, "A", "100", "5", "bid100")
    log.info("不交叉判定：成交数=%d（期望 0），订单入簿", len(r.trades))
    assert r.trades == []
    assert r.status.value == "NEW"
    snap = engine.book_snapshot()
    log.info("簿快照：bids=%s asks=%s", snap["bids"], snap["asks"])
    assert snap["bids"][0]["price"] == "100"
    assert snap["asks"][0]["price"] == "110"


def test_self_trade_prevented(engine):
    _sell(engine, "A", "100", "5", "a-own-sell")  # A 自己挂卖
    with pytest.raises(SelfTradePreventedError) as ei:
        _buy(engine, "A", "100", "5", "a-own-buy")  # A 又想买，交叉
    log.info("自成交判定 reason=%s（期望 SELF_TRADE），簿不应变化", ei.value.reason.value)
    assert ei.value.reason is RejectReason.SELF_TRADE
    snap = engine.book_snapshot()
    assert len(snap["bids"]) == 0  # 被拒买单没有入簿


def test_cancel_unknown_and_duplicate(engine):
    s = _sell(engine, "B", "100", "5", "to-cancel")
    ok = engine.cancel(s.order_id)
    log.info("撤单结果 cancelled=%s", ok.cancelled)
    assert ok.cancelled is True
    with pytest.raises(AlreadyCancelledError) as ei:
        engine.cancel(s.order_id)
    assert ei.value.reason is RejectReason.ALREADY_CANCELLED
    log.info("重复撤单判定 reason=%s（期望 ALREADY_CANCELLED）", ei.value.reason.value)
    with pytest.raises(UnknownOrderError) as ei2:
        engine.cancel("O999")
    assert ei2.value.reason is RejectReason.UNKNOWN_ORDER


def test_cancel_filled_order_rejected(engine):
    s = _sell(engine, "B", "100", "3", "filled-sell")
    _buy(engine, "A", "100", "3", "filling-buy")
    with pytest.raises(OrderNotActiveError) as ei:
        engine.cancel(s.order_id)
    log.info("已成交撤单判定 reason=%s（期望 ORDER_NOT_ACTIVE）", ei.value.reason.value)
    assert ei.value.reason is RejectReason.ORDER_NOT_ACTIVE


def test_cancel_releases_reservation_and_conservation(engine):
    s = _sell(engine, "B", "100", "5", "freeze-sell")
    b_before = engine.get_account("B")
    log.info("挂卖后 B.reserved_sell=%s（期望 5）", b_before.reserved_sell_qty)
    assert b_before.reserved_sell_qty == Decimal("5")
    engine.cancel(s.order_id)
    b_after = engine.get_account("B")
    log.info("撤单后 B.pos=%s reserved=%s（持仓回到 1000/冻结 0）",
             b_after.position, b_after.reserved_sell_qty)
    assert b_after.reserved_sell_qty == Decimal("0")
    assert b_after.position == Decimal("2000")
