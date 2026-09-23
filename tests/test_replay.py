"""可恢复重放测试。

- 重放后订单簿、账户、成交序列与重放前逐字段一致。
- 乱序 seq、非法 JSON、非法/矛盾 payload 必须明确 ReplayError，不得静默跳过。
- 重启后幂等表重建：历史幂等键不重复成交。
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

import pytest

from app.errors import ReplayError
from app.engine import MatchingEngine
from app.models import AccountConfig, Side, SubmitOrderRequest
from app.store import EventStore

log = logging.getLogger("test.replay")


def _fresh(path):
    st = EventStore(path); st.ensure()
    e = MatchingEngine(st); e.replay()
    return e


@pytest.fixture()
def path(tmp_path):
    return tmp_path / "events.jsonl"


def _build_history(engine):
    engine.configure_account(AccountConfig(
        account_id="A", cash=Decimal("1000"), position=Decimal("0"),
        max_order_notional=Decimal("1000"), max_net_position=Decimal("20")))
    engine.configure_account(AccountConfig(
        account_id="B", cash=Decimal("1000"), position=Decimal("10"),
        max_order_notional=Decimal("1000"), max_net_position=Decimal("20")))
    engine.submit(SubmitOrderRequest(account_id="B", side=Side.SELL,
                                     price=Decimal("100"), quantity=Decimal("10"),
                                     idempotency_key="b-sell"))
    engine.submit(SubmitOrderRequest(account_id="A", side=Side.BUY,
                                     price=Decimal("100"), quantity=Decimal("4"),
                                     idempotency_key="a-buy"))
    # B 的卖单部分成交后，再挂一个买单形成两侧订单簿
    engine.submit(SubmitOrderRequest(account_id="A", side=Side.BUY,
                                     price=Decimal("90"), quantity=Decimal("2"),
                                     idempotency_key="a-bid"))


def test_replay_rebuilds_identical_state(path):
    e1 = _fresh(path)
    _build_history(e1)
    before = {
        "book": e1.book_snapshot(),
        "trades": [t.model_dump(mode="json") for t in e1.list_trades()],
        "A": e1.get_account("A").model_dump(mode="json"),
        "B": e1.get_account("B").model_dump(mode="json"),
        "order": e1.get_order("O3").model_dump(mode="json"),
    }
    log.info("重放前关键状态：book=%s trades=%d A=%s B=%s",
             before["book"], len(before["trades"]), before["A"], before["B"])

    e2 = _fresh(path)  # 新引擎读同一文件
    after = {
        "book": e2.book_snapshot(),
        "trades": [t.model_dump(mode="json") for t in e2.list_trades()],
        "A": e2.get_account("A").model_dump(mode="json"),
        "B": e2.get_account("B").model_dump(mode="json"),
        "order": e2.get_order("O3").model_dump(mode="json"),
    }
    log.info("重放后关键状态：book=%s trades=%d", after["book"], len(after["trades"]))
    assert after == before, "重放后状态必须与重放前逐字段一致"


def test_replay_trade_sequence_identical(path):
    e1 = _fresh(path); _build_history(e1)
    e2 = _fresh(path)
    ids1 = [t.trade_id for t in e1.list_trades()]
    ids2 = [t.trade_id for t in e2.list_trades()]
    log.info("成交序列判定：%s == %s", ids1, ids2)
    assert ids1 == ids2 and ids1 == ["T5"]


def test_replay_idempotency_rebuilt(path):
    e1 = _fresh(path); _build_history(e1)
    e2 = _fresh(path)
    trades_before = len(e2.list_trades())
    # 历史幂等键在重启后重复提交：返回首次结果，不新增成交
    r = e2.submit(SubmitOrderRequest(account_id="A", side=Side.BUY,
                                     price=Decimal("100"), quantity=Decimal("4"),
                                     idempotency_key="a-buy"))
    log.info("重启后幂等判定：返回成交数=%d，总成交仍=%d", len(r.trades), trades_before)
    assert len(r.trades) == 1
    assert len(e2.list_trades()) == trades_before


def test_replay_out_of_order_seq_rejected(path):
    e1 = _fresh(path); _build_history(e1)
    lines = path.read_text(encoding="utf-8").splitlines()
    # 交换第 3、4 行，制造 seq 乱序
    lines[2], lines[3] = lines[3], lines[2]
    corrupt = path.parent / "corrupt.jsonl"
    corrupt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    st = EventStore(corrupt)
    e = MatchingEngine(st)
    with pytest.raises(ReplayError) as ei:
        e.replay()
    log.info("乱序事件判定：%s", ei.value)
    assert "乱序" in str(ei.value)


def test_replay_illegal_json_rejected(path):
    e1 = _fresh(path); _build_history(e1)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(2, "{not-json")
    corrupt = path.parent / "badjson.jsonl"
    corrupt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    e = MatchingEngine(EventStore(corrupt))
    with pytest.raises(ReplayError) as ei:
        e.replay()
    log.info("非法 JSON 判定：%s", ei.value)
    assert "JSON" in str(ei.value) or "合法" in str(ei.value)


def test_replay_contradictory_trade_rejected(path):
    e1 = _fresh(path); _build_history(e1)
    lines = path.read_text(encoding="utf-8").splitlines()
    # 找到 TRADE_EXECUTED 行，把成交数量改成超过订单剩余量
    for i, line in enumerate(lines):
        obj = json.loads(line)
        if obj["type"] == "TRADE_EXECUTED":
            obj["payload"]["quantity"] = "999"
            lines[i] = json.dumps(obj, sort_keys=True)
            break
    corrupt = path.parent / "badtrade.jsonl"
    corrupt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    e = MatchingEngine(EventStore(corrupt))
    with pytest.raises(ReplayError) as ei:
        e.replay()
    log.info("矛盾成交（超量）判定：%s", ei.value)
    assert "超过订单剩余量" in str(ei.value)


def test_replay_self_trade_event_rejected(path):
    # 手工构造一条 maker/taker 同账户的成交事件（绕过 live 防护，模拟被污染日志）
    e1 = _fresh(path)
    e1.configure_account(AccountConfig(account_id="X", cash=Decimal("1000"),
                                       position=Decimal("10")))
    # 直接向事件文件写入两条提交 + 一条自成交，seq 分别为 2/3/4
    st = e1.store
    st.append("ORDER_SUBMITTED", {"order_id": "O2", "account_id": "X", "side": "SELL",
                                  "price": "100", "original_qty": "5",
                                  "idempotency_key": None})
    st.append("ORDER_SUBMITTED", {"order_id": "O3", "account_id": "X", "side": "BUY",
                                  "price": "100", "original_qty": "5",
                                  "idempotency_key": None})
    st.append("TRADE_EXECUTED", {"trade_id": "T4", "maker_order_id": "O2",
                                 "taker_order_id": "O3", "maker_account_id": "X",
                                 "taker_account_id": "X", "taker_side": "BUY",
                                 "price": "100", "quantity": "5"})
    e2 = MatchingEngine(EventStore(path))
    with pytest.raises(ReplayError) as ei:
        e2.replay()
    log.info("自成交历史事件判定：%s", ei.value)
    assert "自成交" in str(ei.value)


def test_replay_duplicate_cancel_rejected(path):
    e1 = _fresh(path); _build_history(e1)
    # 撤掉 B 的剩余卖单，然后复制该撤单事件制造重复撤销
    e1.cancel("O3", idempotency_key="cancel-1")
    lines = path.read_text(encoding="utf-8").splitlines()
    cancel_lines = [ln for ln in lines if json.loads(ln)["type"] == "ORDER_CANCELLED"]
    duplicated = lines + cancel_lines  # seq 会同时乱序，先触发乱序
    # 为单独验证“重复撤销”语义，把复制行的 seq 改为追加的正确序号
    last_seq = len(lines)
    obj = json.loads(cancel_lines[0]); obj["seq"] = last_seq + 1
    lines.append(json.dumps(obj, sort_keys=True))
    corrupt = path.parent / "dupcancel.jsonl"
    corrupt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    e = MatchingEngine(EventStore(corrupt))
    with pytest.raises(ReplayError) as ei:
        e.replay()
    log.info("重复撤销事件判定：%s", ei.value)
    assert "重复撤销" in str(ei.value)
