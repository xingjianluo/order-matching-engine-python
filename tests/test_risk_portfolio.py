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


# ============ 预计现金口径：在途卖单成交回款必须计入 ============
@pytest.fixture
def dual():
    """双向挂单账户：trader 现金 2000、BTC 持仓 50；counter 用于对手成交。"""
    from app.service import TradingService
    s = TradingService()
    s.open_account("trader", cash="2000",
                   max_position={"BTC": "1000", "ETH": "1000"})
    s.open_account("counter", cash="100000",
                   max_position={"BTC": "1000", "ETH": "1000"})
    s.register_symbol("BTC")
    s.register_symbol("ETH")
    s.deposit("trader", symbol="BTC", quantity="50")
    s.deposit("counter", symbol="BTC", quantity="1000")
    return s


def test_projected_cash_includes_open_sell_proceeds(dual):
    """同一账户同时挂在途买单与卖单：预计现金与预计持仓同一口径
    （都假设在途订单全部按各自委托价成交）。"""
    logger.info("INPUT: trader cash=2000 BTC=50; buy 4 BTC@100, sell 10 BTC@200, "
                "buy 2 ETH@50（价格不交叉，全部只挂单）")
    b1 = dual.submit_order("d1", "BTC", Side.BUY, "100", "4", "trader")   # 付 400
    s1 = dual.submit_order("d2", "BTC", Side.SELL, "200", "10", "trader")  # 收 2000
    b2 = dual.submit_order("d3", "ETH", Side.BUY, "50", "2", "trader")    # 付 100
    assert b1.status == s1.status == b2.status == OrderStatus.NEW
    assert dual.get_fills() == []  # 买价 < 卖价，不发生自成交

    snap = dual.risk_snapshot("trader")
    logger.info("STATE: cash=%s reserved_buy=%s reserved_sell=%s available=%s "
                "projected=%s", snap.cash, snap.reserved_buy_notional,
                snap.reserved_sell_notional, snap.available_cash,
                snap.projected_cash)
    # 判定依据：预计现金 = 现有现金 − 在途买单应付 + 在途卖单可收
    assert snap.cash == 2000
    assert snap.reserved_buy_notional == 500        # 400 + 100，跨品种共享
    assert snap.reserved_sell_notional == 2000      # 10@200
    assert snap.available_cash == 1500
    assert snap.projected_cash == 2000 - 500 + 2000 == 3500
    # 恒等式：projected_cash == cash - 买占用 + 卖回款
    assert snap.projected_cash == snap.cash - snap.reserved_buy_notional \
        + snap.reserved_sell_notional
    # 同口径的预计持仓：BTC 50 + 4(在途买) − 10(在途卖) = 44
    btc = snap.positions["BTC"]
    assert btc.position == 50 and btc.projected_position == 44
    assert btc.reserved_sell == 10 and btc.available_position == 40
    assert snap.positions["ETH"].projected_position == 2
    # 无任何违约标志
    assert snap.cash_limit_breached is False
    assert snap.projected_cash_limit_breached is False
    assert snap.any_breach is False


def test_projected_values_track_partial_fill_cancel_and_reject(dual):
    """部分成交只释放已成交占用、撤单立即释放、拒单零占用，
    预计现金/预计持仓始终跟随订单簿与账户真实状态。"""
    buy = dual.submit_order("e1", "BTC", Side.BUY, "100", "10", "trader")  # 1000
    sell = dual.submit_order("e2", "BTC", Side.SELL, "150", "5", "trader")  # 750
    assert buy.status == sell.status == OrderStatus.NEW
    snap = dual.risk_snapshot("trader")
    assert snap.projected_cash == 2000 - 1000 + 750 == 1750
    assert snap.positions["BTC"].projected_position == 50 + 10 - 5 == 55

    # 部分成交 4@100：已成交部分转为真实划转，占用只保留剩余 6*100=600
    dual.submit_order("e3", "BTC", Side.SELL, "100", "4", "counter")
    snap = dual.risk_snapshot("trader")
    logger.info("STATE: partial fill 4 -> cash=%s reserved_buy=%s projected=%s",
                snap.cash, snap.reserved_buy_notional, snap.projected_cash)
    assert snap.cash == 1600                       # 真实划出 4*100
    assert snap.reserved_buy_notional == 600       # 只剩 6 口占用
    assert snap.reserved_sell_notional == 750      # 卖单未动
    assert snap.projected_cash == 1600 - 600 + 750 == 1750  # 与成交前一致
    assert snap.positions["BTC"].position == 54
    assert snap.positions["BTC"].projected_position == 55   # 54 + 6 - 5

    # 撤掉在途卖单：750 回款立即从预计现金中释放
    dual.cancel_order(sell.order_id)
    snap = dual.risk_snapshot("trader")
    logger.info("STATE: cancel sell -> reserved_sell=%s projected=%s",
                snap.reserved_sell_notional, snap.projected_cash)
    assert snap.reserved_sell_notional == 0
    assert snap.projected_cash == 1600 - 600 == 1000
    assert snap.positions["BTC"].projected_position == 54 + 6 == 60

    # 拒单零占用：可用现金 1000，买 20@100=2000 被拒，快照不变
    before = dual.risk_snapshot("trader")
    rejected = dual.submit_order("e4", "BTC", Side.BUY, "100", "20", "trader")
    assert rejected.status == OrderStatus.REJECTED
    assert rejected.reject_reason == RejectReason.INSUFFICIENT_CASH
    after = dual.risk_snapshot("trader")
    assert (after.projected_cash, after.reserved_buy_notional,
            after.reserved_sell_notional) == (before.projected_cash,
                                              before.reserved_buy_notional,
                                              before.reserved_sell_notional)
    assert after.positions["BTC"].projected_position == \
        before.positions["BTC"].projected_position

    # 剩余 6 口全部成交后：占用归零，预计值收敛到当前值
    dual.submit_order("e5", "BTC", Side.SELL, "100", "6", "counter")
    snap = dual.risk_snapshot("trader")
    assert snap.reserved_buy_notional == 0 and snap.reserved_sell_notional == 0
    assert snap.cash == 1000
    assert snap.projected_cash == snap.cash == snap.available_cash
    assert snap.positions["BTC"].position == 60
    assert snap.positions["BTC"].projected_position == 60
    logger.info("JUDGEMENT: projected cash/position track real state through "
                "partial fill, cancel and reject")


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


def test_concurrent_mixed_orders_snapshot_identities(tmp_path):
    """同一账户并发双向挂单 + 并发撤单 + 限额收紧交错：
    任一时点快照都必须满足口径恒等式，看不到中间态、不重复占用。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.eventlog import EventLog
    from app.service import TradingService

    svc = TradingService(EventLog(tmp_path / "mixed.jsonl"))
    svc.open_account("trader", cash="100000",
                     max_position={"BTC": "100000", "ETH": "100000"})
    svc.register_symbol("BTC")
    svc.register_symbol("ETH")
    # 每品种入券 1000：足够 50 笔卖单（各 10）占用；持仓远低于限额，
    # 避免限额收紧线程把新单误拒
    svc.deposit("trader", symbol="BTC", quantity="1000")
    svc.deposit("trader", symbol="ETH", quantity="1000")

    symbols = ["BTC", "ETH"]
    stop = threading.Event()
    errors: list[Exception] = []

    def submit(i: int):
        sym = symbols[i % 2]
        if i % 4 < 2:
            # 买单 @10：与卖单 @20 不交叉，全部只挂单
            return svc.submit_order(f"mb{i}", sym, Side.BUY, "10", "10", "trader")
        return svc.submit_order(f"ms{i}", sym, Side.SELL, "20", "10", "trader")

    def cancel(ids):
        for oid in ids:
            try:
                svc.cancel_order(oid)
            except Exception:
                pass  # 与重复撤单竞争时允许业务拒绝

    def tighten():
        # 并发收紧/放宽限额（2000 仍高于最大预计持仓 1500，不会误拒新单）：
        # 只影响违约标志，不得破坏快照恒等式
        while not stop.is_set():
            svc.adjust_limits("trader", max_position={"BTC": "2000"})
            svc.adjust_limits("trader", max_position={"BTC": "100000"})

    def observer():
        while not stop.is_set():
            try:
                snap = svc.risk_snapshot("trader")
                # 口径恒等式 1：可用现金 = 现金 − 在途买单占用
                if snap.available_cash != snap.cash - snap.reserved_buy_notional:
                    raise AssertionError("available cash identity broken")
                # 口径恒等式 2：预计现金 = 现金 − 买占用 + 卖回款
                if snap.projected_cash != snap.cash - snap.reserved_buy_notional \
                        + snap.reserved_sell_notional:
                    raise AssertionError("projected cash identity broken")
                # 口径恒等式 3：分品种可用持仓 = 持仓 − 在途卖占用
                for view in snap.positions.values():
                    if view.available_position != view.position - view.reserved_sell:
                        raise AssertionError(
                            f"{view.symbol} available position identity broken")
                # 占用不为负、不超额（无重复占用）
                if snap.reserved_buy_notional < 0 or snap.reserved_sell_notional < 0:
                    raise AssertionError("negative reservation")
                if snap.reserved_buy_notional > 100000 \
                        or snap.reserved_sell_notional > 20000:
                    raise AssertionError("reservation oversubscribed")
                # 违约标志必须与预计值同口径：负现金 ⇔ 标志置位
                if (snap.projected_cash < 0) != snap.projected_cash_limit_breached:
                    raise AssertionError("projected cash breach flag inconsistent")
                if (snap.cash < 0) != snap.cash_limit_breached:
                    raise AssertionError("cash breach flag inconsistent")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    with ThreadPoolExecutor(max_workers=20) as pool:
        obs = [pool.submit(observer) for _ in range(4)]
        adj = [pool.submit(tighten) for _ in range(2)]
        orders = list(pool.map(submit, range(200)))
        accepted = [o for o in orders if o.status in
                    (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)]
        # 100 笔买（各 100）+ 100 笔卖（各 10 持仓）：资金/持仓充足，全部接受
        assert len(accepted) == 200
        ids = [o.order_id for o in accepted]
        halves = [ids[:100], ids[100:]]
        list(pool.map(cancel, halves))
        stop.set()
        for f in obs + adj:
            f.result()

    assert errors == []
    snap = svc.risk_snapshot("trader")
    logger.info("STATE: after mixed concurrent cancels reserved_buy=%s "
                "reserved_sell=%s projected=%s", snap.reserved_buy_notional,
                snap.reserved_sell_notional, snap.projected_cash)
    # 全部撤完：占用归零，预计值收敛到当前值（无成交）
    assert snap.reserved_buy_notional == 0
    assert snap.reserved_sell_notional == 0
    assert snap.projected_cash == snap.cash == 100000
    assert svc.get_fills() == []


# ============ 重启重放：占用、快照与限额调整保持一致 ============
def _snapshot_dict(svc, account="buyer"):
    snap = svc.risk_snapshot(account)
    return {
        "cash": str(snap.cash),
        "reserved_buy_notional": str(snap.reserved_buy_notional),
        "reserved_sell_notional": str(snap.reserved_sell_notional),
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


def test_mixed_direction_snapshot_identical_after_restart(tmp_path):
    """同一账户同时持有在途买单与卖单：修正后的预计现金/预计持仓、
    限额违约标志在重启重放后与停机前逐字段一致。"""
    from app.eventlog import EventLog
    from app.service import TradingService

    path = tmp_path / "mixed-replay.jsonl"
    svc = TradingService(EventLog(path))
    svc.open_account("trader", cash="5000",
                     max_position={"BTC": "1000", "ETH": "1000"})
    svc.open_account("counter", cash="100000",
                     max_position={"BTC": "1000", "ETH": "1000"})
    svc.register_symbol("BTC")
    svc.register_symbol("ETH")
    svc.deposit("trader", symbol="BTC", quantity="40")
    svc.deposit("counter", symbol="BTC", quantity="1000")

    # 双向在途：买 10@100（付 1000）、卖 5@200（收 1000），价格不交叉
    svc.submit_order("r1", "BTC", Side.BUY, "100", "10", "trader")
    svc.submit_order("r2", "BTC", Side.SELL, "200", "5", "trader")
    # 部分成交 3@100：现金真实划出 300，买单占用只剩 7*100=700
    svc.submit_order("r3", "BTC", Side.SELL, "100", "3", "counter")
    # 收紧 BTC 限额到 40：当前持仓 43 已超限，预计 45 也超限
    svc.adjust_limits("trader", max_position={"BTC": "40"})
    svc._log.close()

    before = _snapshot_dict(svc, "trader")
    logger.info("MIXED SNAPSHOT BEFORE RESTART: %s", before)
    # 修正口径：预计现金 = 4700 − 700(在途买) + 1000(在途卖) = 5000
    assert before["cash"] == "4700"
    assert before["reserved_buy_notional"] == "700"
    assert before["reserved_sell_notional"] == "1000"
    assert before["available_cash"] == "4000"
    assert before["projected_cash"] == "5000"
    btc = before["positions"]["BTC"]
    assert btc["position"] == "43" and btc["projected_position"] == "45"
    assert btc["position_limit_breached"] is True
    assert btc["projected_position_limit_breached"] is True
    assert before["any_breach"] is True

    svc2 = TradingService(EventLog(path))
    svc2.replay()
    after = _snapshot_dict(svc2, "trader")
    logger.info("MIXED SNAPSHOT AFTER RESTART: %s", after)
    assert before == after

    # 重放后限额行为一致：新买单预计持仓 43+7+1-5=46 > 40，仍被拒且零副作用
    blocked = svc2.submit_order("r4", "BTC", Side.BUY, "100", "1", "trader")
    assert blocked.status == OrderStatus.REJECTED
    assert blocked.reject_reason == RejectReason.RISK_LIMIT_EXCEEDED
    still = _snapshot_dict(svc2, "trader")
    assert still["projected_cash"] == "5000"
    assert still["positions"]["BTC"]["projected_position"] == "45"
    logger.info("JUDGEMENT: mixed-direction projected values and limit "
                "behaviour identical after replay")


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
