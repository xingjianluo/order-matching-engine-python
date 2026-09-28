"""组合级风险占用与观测：跨品种共享资金、按品种占用持仓、风险快照、
动态限额生效边界、占用释放、并发一致性与重启重放。

所有用例均通过日志打印输入、关键状态快照与判定依据。
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app.eventlog import EventLog
from app.models import OrderStatus, RejectReason, Side, TradingError
from app.service import TradingService

logger = logging.getLogger("test.portfolio")


def _bootstrap(path=None):
    """两品种 BTC/ETH 多品种仿真账户：fund 现金 1000、BTC 持仓 10。"""
    svc = TradingService(EventLog(path) if path else None)
    svc.open_account("fund", cash="1000",
                     max_position={"BTC": "100", "ETH": "100"})
    svc.open_account("maker", cash="100000",
                     max_position={"BTC": "10000", "ETH": "10000"})
    svc.register_symbol("BTC")
    svc.register_symbol("ETH")
    svc.deposit("fund", symbol="BTC", quantity="10")
    svc.deposit("maker", symbol="BTC", quantity="100")
    svc.deposit("maker", symbol="ETH", quantity="100")
    return svc


# ============ 跨品种在途买单共享同一份现金 ============
def test_cross_symbol_buys_share_cash(tmp_path):
    svc = _bootstrap(tmp_path / "e.jsonl")
    # BTC 挂买 6@100 = 600（簿中无卖盘，挂单不成交）
    o1 = svc.submit_order("b1", "BTC", Side.BUY, "100", "6", "fund")
    assert o1.status == OrderStatus.NEW
    # ETH 再挂买 5@100 = 500：1000 - 600 = 400 可用，必须拒绝
    o2 = svc.submit_order("b2", "ETH", Side.BUY, "100", "5", "fund")
    logger.info("INPUT: reserved BTC 600, ETH buy 500 vs cash 1000")
    assert o2.status == OrderStatus.REJECTED
    assert o2.reject_reason == RejectReason.INSUFFICIENT_CASH
    # 拒绝零副作用：订单簿只有 BTC 买单，ETH 簿为空，无成交，现金不变
    assert svc.book_snapshot("ETH")["bids"] == []
    assert svc.book_snapshot("BTC")["bids"][0]["quantity"] == "6"
    assert svc.get_fills() == []
    assert svc.get_account("fund").cash == 1000

    # 4@100 = 400 恰好可用：接受
    o3 = svc.submit_order("b3", "ETH", Side.BUY, "100", "4", "fund")
    assert o3.status == OrderStatus.NEW
    snap = svc.risk_snapshot("fund")
    logger.info("STATE: reserved=%s available=%s",
                snap.reserved_buy_notional, snap.available_cash)
    assert snap.reserved_buy_notional == Decimal("1000")
    assert snap.available_cash == Decimal("0")


def test_same_symbol_sell_orders_share_available_position(tmp_path):
    svc = _bootstrap(tmp_path / "e.jsonl")
    # 持仓 10：先挂卖 8（在途占用 8），再挂卖 3 应被拒（可用仅 2）
    o1 = svc.submit_order("s1", "BTC", Side.SELL, "9000", "8", "fund")
    assert o1.status == OrderStatus.NEW
    o2 = svc.submit_order("s2", "BTC", Side.SELL, "9000", "3", "fund")
    logger.info("INPUT: position 10, reserved sell 8, second sell 3")
    assert o2.status == OrderStatus.REJECTED
    assert o2.reject_reason == RejectReason.INSUFFICIENT_POSITION
    # 零副作用：第一单仍在，第二单不入簿
    assert svc.book_snapshot("BTC")["asks"][0]["quantity"] == "8"
    assert svc.get_fills() == []
    snap = svc.risk_snapshot("fund")
    btc = snap.symbols["BTC"]
    assert btc.position == 10
    assert btc.reserved_sell == 8
    assert btc.available_position == 2


# ============ 风险快照字段与预估 ============
def test_risk_snapshot_fields_and_projection(tmp_path):
    svc = _bootstrap(tmp_path / "e.jsonl")
    # maker 挂卖单供 fund 成交：BTC 3@100
    svc.submit_order("m1", "BTC", Side.SELL, "100", "3", "maker")
    # fund 买入 3（成交）：现金 700、持仓 13；再挂 BTC 买 2@100（占用 200）
    bought = svc.submit_order("f1", "BTC", Side.BUY, "100", "3", "fund")
    assert bought.status == OrderStatus.FILLED
    svc.submit_order("f2", "BTC", Side.BUY, "100", "2", "fund")   # 在途买
    svc.submit_order("f3", "BTC", Side.SELL, "9000", "4", "fund")  # 在途卖
    # ETH 在途买 1@50（占用 50）
    svc.submit_order("f4", "ETH", Side.BUY, "50", "1", "fund")

    snap = svc.risk_snapshot("fund")
    logger.info("SNAPSHOT: cash=%s reserved=%s avail=%s projected_cash=%s",
                snap.cash, snap.reserved_buy_notional,
                snap.available_cash, snap.projected_cash)
    assert snap.cash == Decimal("700")
    # 在途买单：BTC 2@100=200 + ETH 1@50=50
    assert snap.reserved_buy_notional == Decimal("250")
    assert snap.available_cash == Decimal("450")
    # 预估现金：买单花掉 250，卖单收入 4*9000=36000
    assert snap.projected_cash == Decimal("700") - Decimal("250") + Decimal("36000")

    btc = snap.symbols["BTC"]
    # 当前持仓 13；在途买 2、在途卖 4
    assert btc.position == 13
    assert btc.resting_buy_qty == 2
    assert btc.reserved_sell == 4
    assert btc.available_position == 9
    # 全部成交后预估净持仓 = 13 + 2 - 4 = 11
    assert btc.projected_position == 11
    assert btc.max_position == Decimal("100")
    assert btc.limit_breached is False
    assert btc.projected_limit_breached is False

    eth = snap.symbols["ETH"]
    assert eth.position == 0 and eth.resting_buy_qty == 1
    assert eth.projected_position == 1


def test_tighten_position_limit_rejects_with_zero_side_effect(tmp_path):
    svc = _bootstrap(tmp_path / "e.jsonl")
    # 在途买 6@100，占用现金 600；当前持仓 0
    first = svc.submit_order("t1", "BTC", Side.BUY, "100", "6", "fund")
    assert first.status == OrderStatus.NEW

    # 运行中收紧 BTC 持仓限额：当前持仓 10 + 在途买 6 -> 预估 16 > 15
    svc.adjust_limits("fund", max_position={"BTC": "15"})
    snap = svc.risk_snapshot("fund")
    logger.info("STATE after tighten: projected_pos=%s projected_breached=%s",
                snap.symbols["BTC"].projected_position,
                snap.projected_limit_breached)
    assert snap.projected_limit_breached is True
    assert snap.symbols["BTC"].projected_position == 16

    # 收紧不撤已有在途单：仍在簿中
    assert svc.book_snapshot("BTC")["bids"][0]["quantity"] == "6"

    # 后续新单（再买 1，预估 7）必须拒绝，且零副作用
    before_fills = len(svc.get_fills())
    rejected = svc.submit_order("t2", "BTC", Side.BUY, "100", "1", "fund")
    assert rejected.status == OrderStatus.REJECTED
    assert rejected.reject_reason == RejectReason.RISK_LIMIT_EXCEEDED
    assert len(svc.get_fills()) == before_fills
    assert svc.book_snapshot("BTC")["bids"][0]["quantity"] == "6"
    assert svc.get_account("fund").cash == 1000
    # 被拒单不进在途集合，快照占用不变
    snap2 = svc.risk_snapshot("fund")
    assert snap2.reserved_buy_notional == Decimal("600")


def test_tighten_order_notional_cap_flags_resting_and_blocks_new(tmp_path):
    svc = _bootstrap(tmp_path / "e.jsonl")
    big = svc.submit_order("n1", "BTC", Side.BUY, "100", "4", "fund")  # 名义额 400
    assert big.status == OrderStatus.NEW
    # 收紧单笔上限到 300：旧单不强制撤销
    svc.adjust_limits("fund", max_order_notional="300")
    snap = svc.risk_snapshot("fund")
    assert snap.over_capped_orders == [big.order_id]
    # 新单名义额 350：可用现金 600 足够，但超过单笔上限 300
    rej = svc.submit_order("n2", "ETH", Side.BUY, "350", "1", "fund")
    assert rej.status == OrderStatus.REJECTED
    assert rej.reject_reason == RejectReason.RISK_LIMIT_EXCEEDED
    assert svc.book_snapshot("ETH")["bids"] == []
    # 旧单仍可被吃（不强制取消）：maker 卖 4@100 与之成交
    svc.submit_order("m2", "BTC", Side.SELL, "100", "4", "maker")
    assert svc.get_order(big.order_id).status == OrderStatus.FILLED
    logger.info("JUDGEMENT: tightened cap blocks new orders, resting order survives")


# ============ 撤单 / 部分成交释放占用 ============
def test_cancel_releases_cross_symbol_cash_reservation(tmp_path):
    svc = _bootstrap(tmp_path / "e.jsonl")
    # BTC 在途买 6@100 占用 600
    buy_btc = svc.submit_order("r1", "BTC", Side.BUY, "100", "6", "fund")
    assert svc.risk_snapshot("fund").available_cash == 400
    # 撤单后跨品种现金立即释放：ETH 买 500 可用
    svc.cancel_order(buy_btc.order_id)
    assert svc.risk_snapshot("fund").available_cash == 1000
    eth = svc.submit_order("r2", "ETH", Side.BUY, "100", "5", "fund")
    assert eth.status == OrderStatus.NEW
    logger.info("JUDGEMENT: cancel released shared cash for another symbol")


def test_partial_fill_releases_only_filled_notional(tmp_path):
    svc = _bootstrap(tmp_path / "e.jsonl")
    # maker 在 BTC 卖 3@100；fund 买 6@100 -> 成交 3，剩余 3 在途占用 300
    svc.submit_order("p0", "BTC", Side.SELL, "100", "3", "maker")
    buy = svc.submit_order("p1", "BTC", Side.BUY, "100", "6", "fund")
    assert buy.status == OrderStatus.PARTIALLY_FILLED
    snap = svc.risk_snapshot("fund")
    logger.info("STATE after partial: cash=%s reserved=%s avail=%s",
                snap.cash, snap.reserved_buy_notional, snap.available_cash)
    # 已成交 3 实际扣款 300；在途剩余 3 只占用 300（不是 600）
    assert snap.cash == Decimal("700")
    assert snap.reserved_buy_notional == Decimal("300")
    assert snap.available_cash == Decimal("400")

    # maker 再卖 3，剩余 3 全部成交，占用清零
    svc.submit_order("p2", "BTC", Side.SELL, "100", "3", "maker")
    assert svc.get_order(buy.order_id).status == OrderStatus.FILLED
    snap2 = svc.risk_snapshot("fund")
    assert snap2.reserved_buy_notional == 0
    assert snap2.available_cash == snap2.cash == Decimal("400")


def test_partial_fill_sell_releases_position(tmp_path):
    svc = _bootstrap(tmp_path / "e.jsonl")
    # fund 持仓 10：挂卖 8@9000；maker 买 3@9000 -> 成交 3，剩 5 在途
    svc.submit_order("q0", "BTC", Side.BUY, "9000", "3", "maker")
    sell = svc.submit_order("q1", "BTC", Side.SELL, "9000", "8", "fund")
    assert sell.status == OrderStatus.PARTIALLY_FILLED
    snap = svc.risk_snapshot("fund")
    btc = snap.symbols["BTC"]
    # 当前持仓已被成交扣减到 7；在途卖占用只剩 5；可用 = 7-5 = 2
    logger.info("STATE sell partial: position=%s reserved_sell=%s avail=%s",
                btc.position, btc.reserved_sell, btc.available_position)
    assert btc.position == 7
    assert btc.reserved_sell == 5
    assert btc.available_position == 2


# ============ 并发下单 / 撤单下快照一致 ============
def test_concurrent_submits_snapshot_consistent(tmp_path):
    svc = _bootstrap(tmp_path / "e.jsonl")
    accepted: list = []
    rejected: list = []

    def buy(i: int):
        try:
            # 每单 1@100，现金 1000 => 最多 10 单进入
            o = svc.submit_order(f"cb{i}", "BTC", Side.BUY, "100", "1", "fund")
            if o.status == OrderStatus.NEW:
                accepted.append(o)
            else:
                rejected.append(o)
        except TradingError as exc:
            rejected.append(exc)

    # 下单过程中并发反复取快照，断言“占用 + 可用 == 现金”恒成立且无重复占用
    snapshots: list = []
    stop = threading.Event()

    def observer():
        while not stop.is_set():
            snap = svc.risk_snapshot("fund")
            assert snap.reserved_buy_notional + snap.available_cash == snap.cash
            assert snap.reserved_buy_notional % 100 == 0
            snapshots.append(snap.reserved_buy_notional)

    t = threading.Thread(target=observer)
    t.start()
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(buy, range(60)))
    stop.set()
    t.join()

    logger.info("STATE concurrent: accepted=%d rejected=%d max_reserved_seen=%s",
                len(accepted), len(rejected),
                max(snapshots) if snapshots else None)
    assert len(accepted) == 10
    # 最终态：在途买 10@100 占用 1000，可用现金 0
    snap = svc.risk_snapshot("fund")
    assert snap.reserved_buy_notional == Decimal("1000")
    assert snap.available_cash == 0
    assert svc.book_snapshot("BTC")["bids"][0]["quantity"] == "10"
    assert svc.get_fills() == []


def test_concurrent_cancels_and_snapshots_consistent(tmp_path):
    svc = _bootstrap(tmp_path / "e.jsonl")
    orders = [svc.submit_order(f"cx{i}", "BTC", Side.BUY, "100", "1", "fund")
              for i in range(10)]
    assert svc.risk_snapshot("fund").reserved_buy_notional == 1000

    barrier = threading.Barrier(10)

    def cancel(o):
        barrier.wait()
        svc.cancel_order(o.order_id)

    snaps: list = []

    def observer():
        for _ in range(300):
            snap = svc.risk_snapshot("fund")
            # 不变量：占用恒为 100 的整数倍，且占用+可用==现金
            assert snap.reserved_buy_notional % 100 == 0
            assert snap.reserved_buy_notional + snap.available_cash == snap.cash
            snaps.append(snap.reserved_buy_notional)

    obs = threading.Thread(target=observer)
    obs.start()
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(cancel, orders))
    obs.join()

    final = svc.risk_snapshot("fund")
    logger.info("STATE concurrent cancels: reserved_seen=%s final=%s",
                sorted(set(snaps)), final.reserved_buy_notional)
    assert final.reserved_buy_notional == 0
    assert final.available_cash == 1000
    assert svc.book_snapshot("BTC")["bids"] == []


# ============ 重启重放后结果一致 ============
def test_portfolio_state_persists_across_restart(tmp_path):
    path = tmp_path / "e.jsonl"
    svc = _bootstrap(path)
    # 动态限额（先收紧，再放宽一个品种、重置另一个）
    svc.adjust_limits("fund", max_position={"BTC": "8"})
    svc.adjust_limits("fund", max_order_notional="500")
    svc.adjust_limits("fund", max_position={"BTC": "20"})
    svc.adjust_limits("fund", max_position={"BTC": "20"}, reset_positions=True)

    # maker 卖 4@100；fund 买 4 成交
    svc.submit_order("z0", "BTC", Side.SELL, "100", "4", "maker")
    filled = svc.submit_order("z1", "BTC", Side.BUY, "100", "4", "fund")
    assert filled.status == OrderStatus.FILLED
    # 在途：BTC 买 1@100、ETH 买 2@100；在途卖 BTC 3
    svc.submit_order("z2", "BTC", Side.BUY, "100", "1", "fund")
    svc.submit_order("z3", "ETH", Side.BUY, "100", "2", "fund")
    svc.submit_order("z4", "BTC", Side.SELL, "9000", "3", "fund")
    # 一个被拒单（收紧后现金不够）也要重放一致
    rejected = svc.submit_order("z5", "BTC", Side.BUY, "100", "5", "fund")
    assert rejected.status == OrderStatus.REJECTED
    svc._log.close()

    before = svc.risk_snapshot("fund")

    svc2 = _bootstrap_noevents(path)
    svc2.replay()
    after = svc2.risk_snapshot("fund")
    logger.info("REPLAY before/after reserved %s/%s avail %s/%s projected %s/%s",
                before.reserved_buy_notional, after.reserved_buy_notional,
                before.available_cash, after.available_cash,
                before.projected_cash, after.projected_cash)
    assert after.cash == before.cash
    assert after.reserved_buy_notional == before.reserved_buy_notional
    assert after.available_cash == before.available_cash
    assert after.projected_cash == before.projected_cash
    assert after.max_order_notional == before.max_order_notional
    assert {s: (v.position, v.reserved_sell, v.available_position,
                v.resting_buy_qty, v.projected_position, v.max_position,
                v.projected_limit_breached)
            for s, v in after.symbols.items()} == \
           {s: (v.position, v.reserved_sell, v.available_position,
                v.resting_buy_qty, v.projected_position, v.max_position,
                v.projected_limit_breached)
            for s, v in before.symbols.items()}
    # 订单簿与成交序列也一致
    assert svc2.book_snapshot("BTC") == svc.book_snapshot("BTC")
    assert svc2.book_snapshot("ETH") == svc.book_snapshot("ETH")
    assert len(svc2.get_fills()) == len(svc.get_fills())
    # 重放后收紧限额仍生效：超限新单继续被拦
    again = svc2.submit_order("z6", "BTC", Side.BUY, "100", "5", "fund")
    assert again.status == OrderStatus.REJECTED
    assert again.reject_reason == RejectReason.INSUFFICIENT_CASH


def _bootstrap_noevents(path):
    """与 _bootstrap 相同的服务壳，但不产生任何新事件（供重放使用）。"""
    return TradingService(EventLog(path))
