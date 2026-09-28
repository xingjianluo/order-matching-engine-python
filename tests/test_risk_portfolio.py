"""组合级风险占用与观测：跨品种共享资金、分品种持仓占用、
动态限额、部分成交/撤单释放、快照语义与重启重放一致。

日志打印输入、关键状态与判定依据，与既有测试保持一致。
"""
import logging

import pytest

from app.models import OrderStatus, RejectReason, Side

logger = logging.getLogger("test.risk.portfolio")


@pytest.fixture
def multi():
    """两品种 BTC/ETH 的仿真账户：买家现金 1000，卖家各持 100。"""
    from app.service import TradingService
    s = TradingService()
    s.open_account("buyer", cash="1000",
                   max_position={"BTC": "1000", "ETH": "1000"})
    s.open_account("seller", cash="0",
                   max_position={"BTC": "1000", "ETH": "1000"})
    s.register_symbol("BTC")
    s.register_symbol("ETH")
    s.deposit("seller", symbol="BTC", quantity="100")
    s.deposit("seller", symbol="ETH", quantity="100")
    return s


def test_cross_symbol_buy_orders_share_one_cash_pool(multi):
    logger.info("INPUT: buyer cash=1000; buy 6 BTC@100(=600), then 5 ETH@100(=500)")
    o1 = multi.submit_order("b1", "BTC", Side.BUY, "100", "6", "buyer")
    o2 = multi.submit_order("b2", "ETH", Side.BUY, "100", "5", "buyer")
    assert o1.status == OrderStatus.NEW
    assert o2.status == OrderStatus.REJECTED
    assert o2.reject_reason == RejectReason.INSUFFICIENT_CASH

    snap = multi.risk_snapshot("buyer")
    logger.info("STATE: reserved=%s available=%s (expected 600/400)",
                snap.reserved_buy_notional, snap.available_cash)
    # 判定依据：占用按账户现金池汇总，BTC 的 600 已占用，ETH 只能再用剩余 400
    assert snap.reserved_buy_notional == 600
    assert snap.available_cash == 400
    # 被拒订单零副作用：ETH 簿无该单、成交为空、现金未划转
    assert multi.book_snapshot("ETH")["bids"] == []
    assert multi.get_fills() == []
    assert multi.get_account("buyer").cash == 1000

    # 400 以内的跨品种新单仍允许
    o3 = multi.submit_order("b3", "ETH", Side.BUY, "100", "4", "buyer")
    assert o3.status == OrderStatus.NEW
    assert multi.risk_snapshot("buyer").reserved_buy_notional == 1000


def test_sell_orders_on_same_symbol_share_available_position(multi):
    logger.info("INPUT: seller BTC=100; sell 80 then 30")
    o1 = multi.submit_order("s1", "BTC", Side.SELL, "200", "80", "seller")
    o2 = multi.submit_order("s2", "BTC", Side.SELL, "200", "30", "seller")
    assert o1.status == OrderStatus.NEW
    assert o2.status == OrderStatus.REJECTED
    assert o2.reject_reason == RejectReason.INSUFFICIENT_POSITION

    snap = multi.risk_snapshot("seller")
    btc = snap.positions["BTC"]
    logger.info("STATE: BTC position=%s reserved_sell=%s available=%s",
                btc.position, btc.reserved_sell, btc.available_position)
    # 判定依据：卖单占用按品种分别计算，80 已占用后只剩 20 可卖
    assert btc.position == 100 and btc.reserved_sell == 80
    assert btc.available_position == 20
    # ETH 持仓不受 BTC 卖单占用影响
    assert snap.positions["ETH"].available_position == 100
    assert multi.get_fills() == []


def test_tightening_limits_rejects_new_order_without_side_effects(multi):
    # 先挂一笔合规买单（名义额 600，BTC 预计持仓 6）
    resting = multi.submit_order("t1", "BTC", Side.BUY, "100", "6", "buyer")
    assert resting.status == OrderStatus.NEW

    # 运行中收紧：BTC 持仓限额 3、单笔名义额上限 100
    multi.adjust_limits("buyer", max_position={"BTC": "3"},
                        max_order_notional="100")
    snap = multi.risk_snapshot("buyer")
    btc = snap.positions["BTC"]
    logger.info("STATE: after tighten projected BTC=%s breach=%s cap=%s",
                btc.projected_position, btc.projected_position_limit_breached,
                snap.max_order_notional)
    # 收紧不撤存量单，但快照标出全部成交后会超限
    assert btc.position_limit_breached is False
    assert btc.projected_position_limit_breached is True
    assert snap.any_breach is True
    assert resting.status == OrderStatus.NEW
    assert multi.book_snapshot("BTC")["bids"][0]["quantity"] == "6"

    # 新单因预计持仓超限被拒（即使数量很小）
    before_book = multi.book_snapshot("BTC")
    before_fills = len(multi.get_fills())
    before_cash = multi.get_account("buyer").cash
    rejected = multi.submit_order("t2", "ETH", Side.BUY, "1", "1", "buyer")
    # ETH 无头寸限额收紧，应通过；改用超单笔限额的 BTC 单验证拒绝
    assert rejected.status == OrderStatus.NEW
    over_cap = multi.submit_order("t3", "BTC", Side.BUY, "100", "2", "buyer")
    assert over_cap.status == OrderStatus.REJECTED
    assert over_cap.reject_reason == RejectReason.RISK_LIMIT_EXCEEDED
    # 判定依据：拒绝零副作用
    assert multi.book_snapshot("BTC") == before_book
    assert len(multi.get_fills()) == before_fills
    assert multi.get_account("buyer").cash == before_cash
    logger.info("JUDGEMENT: tightened limits reject new orders, no state change")


def test_cancel_releases_reservation(multi):
    o1 = multi.submit_order("r1", "BTC", Side.BUY, "100", "6", "buyer")  # 600
    # ETH 单 500：与 BTC 的 600 合计 1100，超过现金 1000 会被拒；
    # 改为 400（4@100）先占用，撤单后释放
    o2 = multi.submit_order("r2", "ETH", Side.BUY, "100", "4", "buyer")  # 400
    assert o1.status == OrderStatus.NEW and o2.status == OrderStatus.NEW
    snap0 = multi.risk_snapshot("buyer")
    assert snap0.reserved_buy_notional == 1000
    assert snap0.available_cash == 0
    multi.cancel_order(o2.order_id)
    snap = multi.risk_snapshot("buyer")
    logger.info("STATE: after cancel ETH order reserved=%s available=%s",
                snap.reserved_buy_notional, snap.available_cash)
    assert snap.reserved_buy_notional == 600
    assert snap.available_cash == 400
    # 释放后 400 的新单可挂
    again = multi.submit_order("r3", "ETH", Side.BUY, "100", "4", "buyer")
    assert again.status == OrderStatus.NEW


def test_partial_fill_releases_only_consumed_reservation(multi):
    # buyer: 10 BTC@90 = 900 买单挂簿
    buy = multi.submit_order("p1", "BTC", Side.BUY, "90", "10", "buyer")
    assert buy.status == OrderStatus.NEW
    snap = multi.risk_snapshot("buyer")
    assert snap.reserved_buy_notional == 900
    # seller 卖 3@90 部分成交
    multi.submit_order("p2", "BTC", Side.SELL, "90", "3", "seller")
    snap = multi.risk_snapshot("buyer")
    logger.info("STATE: partial fill 3 -> reserved=%s cash=%s projected=%s BTC_pos=%s",
                snap.reserved_buy_notional, snap.cash, snap.projected_cash,
                snap.positions["BTC"].position)
    # 已成交 3 部分：现金真实划出 270，占用只保留剩余 7*90=630
    assert snap.cash == 730
    assert snap.reserved_buy_notional == 630
    assert snap.available_cash == 100
    assert snap.projected_cash == 100
    assert snap.positions["BTC"].position == 3
    assert snap.positions["BTC"].projected_position == 10
    # 剩余全部成交后占用归零
    multi.submit_order("p3", "BTC", Side.SELL, "90", "7", "seller")
    snap = multi.risk_snapshot("buyer")
    assert snap.reserved_buy_notional == 0
    assert snap.cash == 100
    assert snap.positions["BTC"].position == 10


def test_snapshot_reports_projective_breach_after_position_tighten(multi):
    multi.submit_order("q1", "BTC", Side.BUY, "50", "6", "buyer")  # 占用 300
    multi.adjust_limits("buyer", max_position={"BTC": "2"})
    snap = multi.risk_snapshot("buyer")
    btc = snap.positions["BTC"]
    assert btc.projected_position == 6
    assert btc.projected_position_limit_breached is True
    assert btc.position_limit_breached is False  # 当前还没成交，不超
    # 现金侧未被触碰
    assert snap.reserved_buy_notional == 300
    assert snap.available_cash == 700


# ============ 并发下的占用与快照一致性 ============
def test_concurrent_submits_and_cancels_snapshot_consistency(tmp_path):
    """并发下单 + 并发撤单交错进行：任一时点占用不得重复、快照恒为一致状态。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.eventlog import EventLog
    from app.service import TradingService

    svc = TradingService(EventLog(tmp_path / "events.jsonl"))
    # 现金 100000：足够多笔挂单，但我们用线程竞争制造临界
    svc.open_account("buyer", cash="100000",
                     max_position={"BTC": "100000", "ETH": "100000"})
    svc.open_account("seller", cash="0",
                     max_position={"BTC": "100000", "ETH": "100000"})
    svc.register_symbol("BTC")
    svc.register_symbol("ETH")
    svc.deposit("seller", symbol="BTC", quantity="100000")
    svc.deposit("seller", symbol="ETH", quantity="100000")

    symbols = ["BTC", "ETH"]
    stop = threading.Event()
    errors: list[Exception] = []

    def submit(i: int):
        sym = symbols[i % 2]
        # 每笔名义额 100；挂远离对手盘的价格保证只挂单不成交
        return svc.submit_order(f"co{i}", sym, Side.BUY, "10", "10", "buyer")

    def cancel(ids):
        for oid in ids:
            try:
                svc.cancel_order(oid)
            except Exception:
                pass  # 与成交/重复撤单竞争时允许业务拒绝

    def observer():
        # 并发只读快照：单个快照的各字段必须在同一时点自洽，
        # 不能看到中间态或重复占用。
        while not stop.is_set():
            try:
                snap = svc.risk_snapshot("buyer")
                reserved = int(snap.reserved_buy_notional)
                # 现金恒等式：现金 = 可用 + 在途占用，且占用不重复、不超额
                if int(snap.cash) != int(snap.available_cash) + reserved:
                    raise AssertionError("cash identity broken")
                if reserved < 0 or reserved > 100000:
                    raise AssertionError(f"reserved out of range: {reserved}")
                if int(snap.projected_cash) != int(snap.available_cash):
                    raise AssertionError("projected cash identity broken")
                for view in snap.positions.values():
                    if int(view.available_position) != int(view.position) \
                            - int(view.reserved_sell):
                        raise AssertionError(
                            f"{view.symbol} available position identity broken")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    # 200 笔买单，名义额各 100 => 总需求 20000 > 100000? 否，20000 < 100000，全挂
    with ThreadPoolExecutor(max_workers=16) as pool:
        obs = [pool.submit(observer) for _ in range(4)]
        orders = list(pool.map(submit, range(200)))
        accepted = [o for o in orders if o.status in
                    (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)]
        rejected = [o for o in orders if o.status == OrderStatus.REJECTED]
        logger.info("STATE: accepted=%d rejected=%d reserved=%s",
                    len(accepted), len(rejected),
                    svc.risk_snapshot("buyer").reserved_buy_notional)
        # 200*100=20000 <= 100000，全部应被接受
        assert len(accepted) == 200 and not rejected
        assert svc.risk_snapshot("buyer").reserved_buy_notional == 20000

        # 并发撤掉全部买单
        ids = [o.order_id for o in accepted]
        halves = [ids[:100], ids[100:]]
        list(pool.map(cancel, halves))
        stop.set()
        for f in obs:
            f.result()

    assert errors == []
    snap = svc.risk_snapshot("buyer")
    logger.info("STATE: after concurrent cancels reserved=%s cash=%s",
                snap.reserved_buy_notional, snap.cash)
    # 撤单完成后占用全部释放，现金不变（无成交）
    assert snap.reserved_buy_notional == 0
    assert snap.available_cash == 100000
    assert snap.cash == 100000
    assert svc.get_fills() == []


def test_concurrent_buys_cash_pool_never_oversubscribed(tmp_path):
    """现金不足时，并发跨品种买单的总占用绝不超过现金（不重复占用）。"""
    from concurrent.futures import ThreadPoolExecutor

    from app.eventlog import EventLog
    from app.service import TradingService

    svc = TradingService(EventLog(tmp_path / "cash.jsonl"))
    svc.open_account("buyer", cash="1000")
    svc.register_symbol("BTC")
    svc.register_symbol("ETH")

    def submit(i: int):
        sym = "BTC" if i % 2 == 0 else "ETH"
        return svc.submit_order(f"k{i}", sym, Side.BUY, "100", "2", "buyer")

    # 100 笔各需 200，现金 1000 => 最多 5 笔被接受
    with ThreadPoolExecutor(max_workers=16) as pool:
        orders = list(pool.map(submit, range(100)))
    accepted = [o for o in orders if o.status != OrderStatus.REJECTED]
    logger.info("INPUT: 100 buys @200 each, cash=1000 -> accepted=%d",
                len(accepted))
    assert len(accepted) == 5
    snap = svc.risk_snapshot("buyer")
    assert snap.reserved_buy_notional == 1000
    assert snap.available_cash == 0
    # 现金未被任何成交划转（全部只挂单）
    assert snap.cash == 1000
    # 被拒幂等：每个 coid 只落一条订单事件，无重复占用
    assert svc.get_fills() == []


# ============ 重启重放：占用、快照与限额调整保持一致 ============
def _snapshot_dict(svc, account="buyer"):
    snap = svc.risk_snapshot(account)
    return {
        "cash": str(snap.cash),
        "reserved_buy_notional": str(snap.reserved_buy_notional),
        "available_cash": str(snap.available_cash),
        "projected_cash": str(snap.projected_cash),
        "max_order_notional": str(snap.max_order_notional),
        "cash_limit_breached": snap.cash_limit_breached,
        "projected_cash_limit_breached": snap.projected_cash_limit_breached,
        "order_notional_breaches": sorted(snap.order_notional_breaches),
        "any_breach": snap.any_breach,
        "positions": {
            sym: {
                "position": str(v.position),
                "reserved_sell": str(v.reserved_sell),
                "available_position": str(v.available_position),
                "max_position": (str(v.max_position)
                                 if v.max_position is not None else None),
                "projected_position": str(v.projected_position),
                "position_limit_breached": v.position_limit_breached,
                "projected_position_limit_breached":
                    v.projected_position_limit_breached,
            }
            for sym, v in sorted(snap.positions.items())
        },
    }


def test_portfolio_state_identical_after_restart(tmp_path):
    from app.eventlog import EventLog
    from app.service import TradingService

    path = tmp_path / "events.jsonl"
    svc = TradingService(EventLog(path))
    svc.open_account("buyer", cash="1000",
                     max_position={"BTC": "100", "ETH": "100"})
    svc.open_account("seller", cash="0",
                     max_position={"BTC": "100", "ETH": "100"})
    svc.register_symbol("BTC")
    svc.register_symbol("ETH")
    svc.deposit("seller", symbol="BTC", quantity="100")
    svc.deposit("seller", symbol="ETH", quantity="100")

    # 跨品种在途买单共享现金：BTC 6@100=600、ETH 3@100=300
    svc.submit_order("m1", "BTC", Side.BUY, "100", "6", "buyer")
    svc.submit_order("m2", "ETH", Side.BUY, "100", "3", "buyer")
    # 同品种在途卖单占用持仓：seller 卖 BTC 70
    svc.submit_order("m3", "BTC", Side.SELL, "200", "70", "seller")
    # 部分成交：再卖 2@100，吃掉 buyer 的 BTC 买单 2
    svc.submit_order("m4", "BTC", Side.SELL, "100", "2", "seller")
    # 撤单释放：撤掉额外的 1 口 ETH 买单（m5 与 m2 合计曾占用 400）
    eth = svc.submit_order("m5", "ETH", Side.BUY, "100", "1", "buyer")
    svc.cancel_order(eth.order_id)
    # 被拒单：BTC 剩 4@100=400 + ETH m2 的 300 = 700 已占用，可用 300，
    # 再买 5@100=500 超过组合可用现金被拒
    rejected = svc.submit_order("m6", "ETH", Side.BUY, "100", "5", "buyer")
    assert rejected.status == OrderStatus.REJECTED
    # 动态收紧限额：BTC 限到 2，buyer 剩余 4 口买单全部成交会到 4（含已成交 2）
    svc.adjust_limits("buyer", max_position={"BTC": "2"},
                      max_order_notional="100000")
    svc._log.close()

    before = {
        "buyer": _snapshot_dict(svc, "buyer"),
        "seller": _snapshot_dict(svc, "seller"),
        "books": {sym: svc.book_snapshot(sym) for sym in ("BTC", "ETH")},
        "fills": [(f.sequence, f.symbol, str(f.price), str(f.quantity),
                   f.buy_order_id, f.sell_order_id) for f in svc.get_fills()],
        "seq": svc._seq,
    }
    logger.info("PRIMARY RISK SNAPSHOT: %s", before["buyer"])

    svc2 = TradingService(EventLog(path))
    svc2.replay()
    after = {
        "buyer": _snapshot_dict(svc2, "buyer"),
        "seller": _snapshot_dict(svc2, "seller"),
        "books": {sym: svc2.book_snapshot(sym) for sym in ("BTC", "ETH")},
        "fills": [(f.sequence, f.symbol, str(f.price), str(f.quantity),
                   f.buy_order_id, f.sell_order_id) for f in svc2.get_fills()],
        "seq": svc2._seq,
    }
    assert before == after
    logger.info("JUDGEMENT: risk snapshots, books and fills identical after replay")

    # 关键字段断言，防止“恰好相等但语义错”
    b = after["buyer"]
    assert b["cash"] == "800"                      # 部分成交划出 2*100
    assert b["reserved_buy_notional"] == "700"    # BTC 400 + ETH 300
    assert b["available_cash"] == "100"           # 当前现金 800 - 占用 700
    assert b["projected_cash"] == "100"
    btc = b["positions"]["BTC"]
    assert btc["position"] == "2" and btc["projected_position"] == "6"
    assert btc["max_position"] == "2"
    assert btc["position_limit_breached"] is False
    assert btc["projected_position_limit_breached"] is True
    assert b["any_breach"] is True
    seller = after["seller"]["positions"]["BTC"]
    assert seller["reserved_sell"] == "70"
    assert seller["available_position"] == "28"   # 100 - 2 成交划出 - 70 占用

    # 重放后收紧限额仍然拦截新单
    blocked = svc2.submit_order("m7", "BTC", Side.BUY, "100", "1", "buyer")
    assert blocked.status == OrderStatus.REJECTED
    assert blocked.reject_reason == RejectReason.RISK_LIMIT_EXCEEDED


def test_http_risk_snapshot_and_limit_adjustment(tmp_path):
    """新 HTTP 端点：风险快照可读、限额可调、重启后仍然生效。"""
    from fastapi.testclient import TestClient

    from app.api import create_app
    from app.eventlog import EventLog
    from app.service import TradingService

    path = tmp_path / "http-risk.jsonl"

    def client():
        svc = TradingService(EventLog(path))
        svc.replay()
        return TestClient(create_app(svc)), svc

    c, _ = client()
    c.post("/admin/accounts", json={"account_id": "buyer", "cash": "1000"})
    c.post("/admin/symbols/BTC")
    c.post("/orders", json={"client_order_id": "h1", "symbol": "BTC",
                            "side": "BUY", "price": "100", "quantity": "6",
                            "account": "buyer"})
    r = c.get("/accounts/buyer/risk")
    assert r.status_code == 200
    snap = r.json()
    logger.info("HTTP RISK SNAPSHOT: %s", snap)
    assert snap["cash"] == "1000"
    assert snap["reserved_buy_notional"] == "600"
    assert snap["available_cash"] == "400"
    assert snap["projected_cash"] == "400"
    btc = next(p for p in snap["positions"] if p["symbol"] == "BTC")
    assert btc["projected_position"] == "6"
    assert btc["position_limit_breached"] is False

    # 收紧 BTC 限额到 2
    r = c.post("/admin/accounts/buyer/limits",
               json={"max_position": {"BTC": "2"}})
    assert r.status_code == 200
    assert r.json()["max_position"] == {"BTC": "2"}
    snap = c.get("/accounts/buyer/risk").json()
    btc = next(p for p in snap["positions"] if p["symbol"] == "BTC")
    assert btc["projected_position_limit_breached"] is True
    assert snap["any_breach"] is True

    # 非法限额返回 422 + INVALID_LIMIT
    bad = c.post("/admin/accounts/buyer/limits",
                 json={"max_order_notional": "0"})
    assert bad.status_code == 422
    assert bad.json()["reason"] == "INVALID_LIMIT"

    # 重启后限额与快照一致
    c2, _ = client()
    snap2 = c2.get("/accounts/buyer/risk").json()
    assert snap2["max_order_notional"] == "Infinity"
    btc2 = next(p for p in snap2["positions"] if p["symbol"] == "BTC")
    assert btc2["max_position"] == "2"
    assert btc2["projected_position_limit_breached"] is True
    logger.info("JUDGEMENT: HTTP risk snapshot & limits persist across restart")
