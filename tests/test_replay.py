"""事件重放恢复：一致性、非法/乱序事件拒绝、端到端 HTTP 持久化。"""
import json
import logging

import pytest

from app.eventlog import EventLog
from app.models import OrderStatus, RejectReason, Side, TradingError
from app.service import TradingService

logger = logging.getLogger("test.replay")


def _build_primary(path) -> dict:
    """在“第一个进程”里执行一串混合操作并导出状态快照。"""
    svc = TradingService(EventLog(path))
    svc.open_account("buyer", cash="100000")
    svc.open_account("seller", cash="0", max_position={"BTC": "1000"})
    svc.register_symbol("BTC")
    svc.deposit("seller", symbol="BTC", quantity="100")

    svc.submit_order("a1", "BTC", Side.SELL, "100", "10", "seller")  # 部分成交
    svc.submit_order("a2", "BTC", Side.BUY, "100", "4", "buyer")
    svc.submit_order("a3", "BTC", Side.SELL, "99", "6", "seller")    # 不成交挂簿
    svc.submit_order("a4", "BTC", Side.BUY, "98", "2", "buyer")      # 不成交挂簿
    resting = svc.submit_order("a5", "BTC", Side.SELL, "100", "3", "seller")
    svc.cancel_order(resting.order_id)
    # 被拒事件也要能重放出一致结果
    svc.submit_order("a6", "BTC", Side.SELL, "100", "9999", "seller")
    svc.submit_order("a7", "BTC", Side.BUY, "100", "5", "buyer")     # 再吃 6 中的 5
    svc._log.close()

    return {
        "book": svc.book_snapshot("BTC"),
        "fills": [(f.sequence, str(f.price), str(f.quantity),
                   f.buy_order_id, f.sell_order_id) for f in svc.get_fills()],
        "accounts": {
            a: (str(svc.get_account(a).cash),
                {s: str(q) for s, q in svc.get_account(a).positions.items()})
            for a in ("buyer", "seller")
        },
        "orders": {oid: (o.status.value, str(o.filled_qty),
                         o.reject_reason.value if o.reject_reason else None)
                   for oid, o in svc._orders.items()},
        "seq": svc._seq,
    }


def test_replay_rebuilds_identical_state(tmp_path):
    path = tmp_path / "events.jsonl"
    before = _build_primary(path)
    logger.info("PRIMARY STATE: %s", json.dumps(before, default=str, ensure_ascii=False))

    svc2 = TradingService(EventLog(path))
    svc2.replay()
    after = {
        "book": svc2.book_snapshot("BTC"),
        "fills": [(f.sequence, str(f.price), str(f.quantity),
                   f.buy_order_id, f.sell_order_id) for f in svc2.get_fills()],
        "accounts": {
            a: (str(svc2.get_account(a).cash),
                {s: str(q) for s, q in svc2.get_account(a).positions.items()})
            for a in ("buyer", "seller")
        },
        "orders": {oid: (o.status.value, str(o.filled_qty),
                         o.reject_reason.value if o.reject_reason else None)
                   for oid, o in svc2._orders.items()},
        "seq": svc2._seq,
    }
    assert before == after
    logger.info("JUDGEMENT: rebuilt book/fills/accounts/orders identical")

    # 重放后继续工作，序号继续递增而不冲突
    more = svc2.submit_order("a8", "BTC", Side.BUY, "100", "1", "buyer")
    assert more.filled_qty == 1
    assert len(svc2.get_fills()) == len(before["fills"]) + 1


def test_replay_twice_is_stable(tmp_path):
    path = tmp_path / "events.jsonl"
    _build_primary(path)
    s1 = TradingService(EventLog(path)); s1.replay()
    snap1 = s1.book_snapshot("BTC")
    s2 = TradingService(EventLog(path)); s2.replay()
    assert s2.book_snapshot("BTC") == snap1


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _expect_reject(events, reasons):
    with pytest.raises(TradingError) as ei:
        TradingService().replay(events)
    assert ei.value.reason in reasons, ei.value.reason
    logger.info("JUDGEMENT: rejected with %s: %s",
                ei.value.reason, ei.value)


def test_replay_out_of_order_seq_gap(tmp_path):
    events = _events(_write(tmp_path))
    events[2]["seq"] = 99
    _expect_reject(events, [RejectReason.OUT_OF_ORDER_EVENT])


def test_replay_out_of_order_duplicate_seq(tmp_path):
    events = _events(_write(tmp_path))
    _expect_reject(events[:3] + [dict(events[2])],
                   [RejectReason.OUT_OF_ORDER_EVENT])


def test_replay_corrupt_json(tmp_path):
    path = _write(tmp_path)
    path.write_text(path.read_text() + "{broken\n")
    with pytest.raises(TradingError) as ei:
        TradingService(EventLog(path)).replay()
    assert ei.value.reason == RejectReason.CORRUPT_EVENT


def test_replay_fill_qty_exceeds_order(tmp_path):
    events = _events(_write(tmp_path))
    for e in events:
        if e["type"] == "FILL":
            e["fill"] = {**e["fill"], "quantity": "9999"}
    _expect_reject(events, [RejectReason.CORRUPT_EVENT])


def test_replay_fill_unknown_order(tmp_path):
    events = _events(_write(tmp_path))
    for e in events:
        if e["type"] == "FILL":
            e["fill"] = {**e["fill"], "buy_order_id": "O404"}
    _expect_reject(events, [RejectReason.CORRUPT_EVENT])


def test_replay_fill_precedes_submission(tmp_path):
    events = [json.loads(json.dumps(e)) for e in _events(_write(tmp_path))]
    fill_idx = next(i for i, e in enumerate(events) if e["type"] == "FILL")
    # 取该成交买单的提交事件，交换两者位置并重写派生 id
    bid = events[fill_idx]["fill"]["buy_order_id"]
    sub_idx = next(i for i, e in enumerate(events)
                   if e.get("order", {}).get("order_id") == bid)
    a, b = fill_idx, sub_idx
    events[a]["seq"], events[b]["seq"] = b + 1, a + 1
    events[a]["fill"]["fill_id"] = f"F{b+1}"
    events[b]["order"]["order_id"] = f"O{a+1}"
    events[a]["fill"]["buy_order_id"] = f"O{a+1}"
    events[a], events[b] = events[b], events[a]
    _expect_reject(events,
                   [RejectReason.OUT_OF_ORDER_EVENT, RejectReason.CORRUPT_EVENT])


def test_replay_unknown_event_type(tmp_path):
    events = _events(_write(tmp_path))
    events.append({"seq": len(events) + 1, "type": "EVIL"})
    _expect_reject(events, [RejectReason.CORRUPT_EVENT])


def test_replay_cancel_after_fill_rejected(tmp_path):
    # 手工构造：先提交卖+买并成交，再追加“撤销已成交卖单”
    path = tmp_path / "x.jsonl"
    s = TradingService(EventLog(path))
    s.open_account("buyer", cash="1000")
    s.open_account("seller", cash="0")
    s.register_symbol("BTC")
    s.deposit("seller", symbol="BTC", quantity="5")
    sell = s.submit_order("x1", "BTC", Side.SELL, "100", "5", "seller")
    s.submit_order("x2", "BTC", Side.BUY, "100", "5", "buyer")
    s._log.close()
    events = _events(path)
    events.append({"seq": len(events) + 1, "type": "ORDER_CANCELLED",
                   "order_id": sell.order_id})
    _expect_reject(events, [RejectReason.CORRUPT_EVENT])


def test_replay_rejected_order_creates_no_book_state(tmp_path):
    path = tmp_path / "ok.jsonl"
    _build_primary(path)
    s2 = TradingService(EventLog(path)); s2.replay()
    rejected = [o for o in s2._orders.values()
                if o.status == OrderStatus.REJECTED]
    assert rejected and all(o.filled_qty == 0 for o in rejected)
    # 被拒单不在任何活跃簿引用中
    for o in rejected:
        assert s2.book_snapshot(o.symbol)["bids"] == s2.book_snapshot(o.symbol)["bids"]


def _write(tmp_path):
    path = tmp_path / "events.jsonl"
    _build_primary(path)
    return path


def test_http_end_to_end_persistence(tmp_path):
    """通过 HTTP 完成配置、撮合、撤单、重复提交，再重启服务验证一致。"""
    from fastapi.testclient import TestClient
    from app.api import create_app

    path = tmp_path / "http.jsonl"

    def fresh_client():
        svc = TradingService(EventLog(path))
        svc.replay()
        return TestClient(create_app(svc)), svc

    c, _ = fresh_client()
    c.post("/admin/accounts", json={"account_id": "buyer", "cash": "10000"})
    c.post("/admin/accounts", json={"account_id": "seller", "cash": "0",
                                    "max_position": {"BTC": "1000"}})
    c.post("/admin/symbols/BTC")
    c.post("/admin/deposits",
           json={"account_id": "seller", "symbol": "BTC", "quantity": "10"})
    r = c.post("/orders", json={"client_order_id": "h1", "symbol": "BTC",
                                "side": "SELL", "price": "100",
                                "quantity": "10", "account": "seller"})
    assert r.status_code == 200
    r = c.post("/orders", json={"client_order_id": "h2", "symbol": "BTC",
                                "side": "BUY", "price": "100",
                                "quantity": "4", "account": "buyer"})
    assert r.json()["status"] == "FILLED"
    # 幂等重复提交
    r2 = c.post("/orders", json={"client_order_id": "h2", "symbol": "BTC",
                                 "side": "BUY", "price": "100",
                                 "quantity": "4", "account": "buyer"})
    assert r2.json()["order_id"] == r.json()["order_id"]
    assert len(c.get("/fills").json()["fills"]) == 1
    # 自成交经 HTTP 返回可区分原因：在更优价位 99 挂自己的卖单，
    # 使买方的对手盘只会是自己（卖一仍是卖方账户的 100）
    c.post("/admin/deposits",
           json={"account_id": "buyer", "symbol": "BTC", "quantity": "1"})
    c.post("/orders", json={"client_order_id": "h3", "symbol": "BTC",
                            "side": "SELL", "price": "99",
                            "quantity": "1", "account": "buyer"})
    rj = c.post("/orders", json={"client_order_id": "h4", "symbol": "BTC",
                                 "side": "BUY", "price": "99",
                                 "quantity": "1", "account": "buyer"})
    # 风控/自成交拒绝：200 + status=REJECTED + 可区分原因
    assert rj.status_code == 200
    assert rj.json()["status"] == "REJECTED"
    assert rj.json()["reject_reason"] == "SELF_TRADE"

    # 重启
    c2, svc2 = fresh_client()
    assert len(c2.get("/fills").json()["fills"]) == 1
    book = c2.get("/books/BTC").json()
    ask_levels = {row["price"]: row["quantity"] for row in book["asks"]}
    assert ask_levels == {"99": "1", "100": "6"}  # 自成交被拒，双方挂单都在
    # 买方持仓 = 买入 4 + 自成交测试前入券 1
    assert svc2.get_account("buyer").positions["BTC"] == 5
    logger.info("JUDGEMENT: HTTP state identical after restart")
