"""并发提交/撤单/查询竞争、超限下单与重复提交幂等。"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.models import OrderStatus, RejectReason, Side
from app.service import TradingService

logger = logging.getLogger("test.concurrency")


def _bootstrap(tmp_path):
    from app.eventlog import EventLog
    svc = TradingService(EventLog(tmp_path / "events.jsonl"))
    svc.open_account("buyer", cash="100000")
    svc.open_account("seller", cash="0",
                     max_position={"BTC": "1000"})
    svc.register_symbol("BTC")
    svc.deposit("seller", symbol="BTC", quantity="1000")
    return svc


def test_risk_limit_blocks_oversized_order(service):
    # 账户级持仓限额：buyer 最多持有 8 BTC
    buyer = service.get_account("buyer")
    buyer.max_position["BTC"] = 8
    logger.info("INPUT: limit=8, buy 10 BTC must be rejected")
    order = service.submit_order("r1", "BTC", Side.BUY, "100", "10", "buyer")
    assert order.status == OrderStatus.REJECTED
    assert order.reject_reason == RejectReason.RISK_LIMIT_EXCEEDED
    # 判定依据：被拒请求不改变订单簿/账户
    assert service.book_snapshot("BTC")["bids"] == []
    assert service.get_account("buyer").cash == 100000
    assert service.get_fills() == []
    logger.info("JUDGEMENT: over-limit rejected, book/cash unchanged")

    # 限额内（8）可以成交：卖家先挂 8@100
    service.submit_order("r0", "BTC", Side.SELL, "100", "8", "seller")
    ok = service.submit_order("r2", "BTC", Side.BUY, "100", "8", "buyer")
    assert ok.status == OrderStatus.FILLED
    assert service.get_account("buyer").positions["BTC"] == 8


def test_reserved_sell_qty_blocks_double_spend(service):
    # 持仓 100：先挂卖 80（不与任何买盘成交），再卖 30 应因可用不足被拒
    first = service.submit_order("rs1", "BTC", Side.SELL, "500", "80", "seller")
    assert first.status == OrderStatus.NEW
    second = service.submit_order("rs2", "BTC", Side.SELL, "500", "30", "seller")
    assert second.status == OrderStatus.REJECTED
    assert second.reject_reason == RejectReason.INSUFFICIENT_POSITION
    # 撤掉首单释放后，30 可挂
    service.cancel_order(first.order_id)
    third = service.submit_order("rs3", "BTC", Side.SELL, "500", "30", "seller")
    assert third.status == OrderStatus.NEW
    logger.info("JUDGEMENT: reservation released after cancel, sell accepted")


def test_concurrent_cancel_vs_fill_only_one_wins(tmp_path):
    svc = _bootstrap(tmp_path)
    sell = svc.submit_order("cf1", "BTC", Side.SELL, "100", "5", "seller")

    outcomes = {}

    def cancel():
        try:
            outcomes["cancel"] = svc.cancel_order(sell.order_id).status
        except Exception as exc:
            outcomes["cancel"] = exc.reason

    def buy():
        outcomes["buy"] = svc.submit_order(
            "cf2", "BTC", Side.BUY, "100", "5", "buyer").status

    t1, t2 = threading.Thread(target=cancel), threading.Thread(target=buy)
    t1.start(); t2.start(); t1.join(); t2.join()
    logger.info("INPUT: cancel & marketable buy race on %s", sell.order_id)
    logger.info("STATE: cancel=%s buy=%s sell_final=%s fills=%d",
                outcomes.get("cancel"), outcomes.get("buy"),
                svc.get_order(sell.order_id).status, len(svc.get_fills()))

    final = svc.get_order(sell.order_id)
    # 判定依据：串行化后只有一个结果——要么撤单成功且无成交，要么完全成交且撤单被拒
    if final.status == OrderStatus.CANCELLED:
        assert len(svc.get_fills()) == 0
        assert outcomes["buy"] in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)
    else:
        assert final.status == OrderStatus.FILLED
        assert len(svc.get_fills()) == 1
        assert outcomes["cancel"] == RejectReason.ORDER_ALREADY_FILLED
    logger.info("JUDGEMENT: exactly one side won, no double state change")


def test_concurrent_buys_never_oversell_resting_qty(tmp_path):
    svc = _bootstrap(tmp_path)
    # 卖盘只有 10，100 个买单各要 1，现金充足，卖方持仓 1000
    svc.submit_order("cc1", "BTC", Side.SELL, "100", "10", "seller")

    def buy(i: int):
        return svc.submit_order(f"ccb{i}", "BTC", Side.BUY, "100", "1", "buyer")

    with ThreadPoolExecutor(max_workers=16) as pool:
        orders = list(pool.map(buy, range(100)))
    filled = [o for o in orders if o.status == OrderStatus.FILLED]
    partial = [o for o in orders if o.status == OrderStatus.PARTIALLY_FILLED]
    # 判定依据：成交量严格守恒——完全成交最多 10 笔，无超卖
    total = sum(o.filled_qty for o in orders)
    logger.info("STATE: filled=%d partial=%d total_filled_qty=%s asks=%s",
                len(filled), len(partial), total,
                svc.book_snapshot("BTC")["asks"])
    assert total == 10
    assert len(filled) == 10
    assert svc.book_snapshot("BTC")["asks"] == []
    assert service_fills_conserved(svc)


def service_fills_conserved(svc) -> bool:
    per_order = {}
    for f in svc.get_fills():
        per_order[f.buy_order_id] = per_order.get(f.buy_order_id, 0) + f.quantity
        per_order[f.sell_order_id] = per_order.get(f.sell_order_id, 0) + f.quantity
    for oid, q in per_order.items():
        if q > svc.get_order(oid).quantity:
            return False
    return True


def test_concurrent_same_idempotency_key_single_fill(tmp_path):
    svc = _bootstrap(tmp_path)
    svc.submit_order("dup-ask", "BTC", Side.SELL, "100", "5", "seller")

    def submit():
        return svc.submit_order("dup-key", "BTC", Side.BUY, "100", "5", "buyer")

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: submit(), range(50)))
    ids = {o.order_id for o in results}
    logger.info("STATE: 50 concurrent submits -> distinct order ids=%s, fills=%d",
                ids, len(svc.get_fills()))
    assert ids == {results[0].order_id}
    assert len(svc.get_fills()) == 1
    assert svc.get_fills()[0].quantity == 5
    logger.info("JUDGEMENT: same idempotency key => consistent result, no dup fill")


def test_same_idempotency_key_different_payload_rejected(service):
    service.submit_order("id1", "BTC", Side.BUY, "100", "1", "buyer")
    with pytest.raises(Exception) as ei:
        service.submit_order("id1", "BTC", Side.BUY, "101", "1", "buyer")
    assert ei.value.reason == RejectReason.DUPLICATE_IDEMPOTENCY_KEY


def test_concurrent_queries_see_consistent_state(tmp_path):
    svc = _bootstrap(tmp_path)
    errors = []

    def worker(i):
        try:
            if i % 3 == 0:
                svc.submit_order(f"q{i}", "BTC", Side.SELL, "200", "1", "seller")
            elif i % 3 == 1:
                svc.book_snapshot("BTC")
            else:
                svc.get_fills()
        except Exception as exc:  # 查询不应出现内部错误
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(worker, range(300)))
    assert errors == []
    logger.info("JUDGEMENT: 300 mixed ops without internal errors")
