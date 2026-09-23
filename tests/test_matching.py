"""正常撮合、部分成交与边界场景，以及数量/资金/持仓守恒。"""
import logging

import pytest

from app.models import OrderStatus, RejectReason, Side, TradingError

logger = logging.getLogger("test.matching")


def test_marketable_buy_full_match(service):
    logger.info("INPUT: seller SELL 10@100, buyer BUY 10@100")
    sell = service.submit_order("s1", "BTC", Side.SELL, "100", "10", "seller")
    buy = service.submit_order("b1", "BTC", Side.BUY, "100", "10", "buyer")

    logger.info("STATE: sell=%s filled=%s; buy=%s filled=%s",
                sell.status, sell.filled_qty, buy.status, buy.filled_qty)
    assert buy.status == OrderStatus.FILLED
    assert sell.status == OrderStatus.FILLED
    fills = service.get_fills()
    assert len(fills) == 1
    # 判定依据：成交价取挂单价（卖一价）
    assert fills[0].price == 100 and fills[0].quantity == 10
    # 守恒：买券=卖券、现金只划转
    assert service.get_account("buyer").positions["BTC"] == 10
    assert service.get_account("seller").positions["BTC"] == 90
    assert service.get_account("buyer").cash == 99000
    assert service.get_account("seller").cash == 1000


def test_price_time_priority(service):
    logger.info("INPUT: asks 10@100(o1) 5@100(o2) 7@99(o3); buy 20@100")
    o1 = service.submit_order("s1", "BTC", Side.SELL, "100", "10", "seller")
    o2 = service.submit_order("s2", "BTC", Side.SELL, "100", "5", "seller")
    o3 = service.submit_order("s3", "BTC", Side.SELL, "99", "7", "seller")
    buy = service.submit_order("b1", "BTC", Side.BUY, "100", "20", "buyer")

    # 判定依据：价格优先 -> 先吃 99 的 7；同价 100 时间优先 -> o1 的 10 再 o2 的 3
    logger.info("STATE: o3 filled=%s, o1 filled=%s, o2 filled=%s, buy remaining=%s",
                o3.filled_qty, o1.filled_qty, o2.filled_qty, buy.remaining)
    assert o3.filled_qty == 7 and o3.status == OrderStatus.FILLED
    assert o1.filled_qty == 10 and o1.status == OrderStatus.FILLED
    assert o2.filled_qty == 3 and o2.status == OrderStatus.PARTIALLY_FILLED
    assert buy.filled_qty == 20 and buy.status == OrderStatus.FILLED
    # 成交总量守恒
    assert sum(f.quantity for f in service.get_fills()) == 20
    book = service.book_snapshot("BTC")
    assert book["asks"][0]["price"] == "100"
    assert book["asks"][0]["quantity"] == "2"


def test_partial_fill_then_remaining_rests(service):
    sell = service.submit_order("s1", "BTC", Side.SELL, "100", "3", "seller")
    buy = service.submit_order("b1", "BTC", Side.BUY, "100", "10", "buyer")
    assert buy.status == OrderStatus.PARTIALLY_FILLED
    assert buy.filled_qty == 3 and buy.remaining == 7
    assert sell.status == OrderStatus.FILLED
    # 买单剩余 7 留在买盘
    assert service.book_snapshot("BTC")["bids"][0]["quantity"] == "7"

    # 后来的卖单与之撮合，产生第二笔成交
    sell2 = service.submit_order("s2", "BTC", Side.SELL, "99", "7", "seller")
    assert sell2.status == OrderStatus.FILLED
    assert buy.status == OrderStatus.FILLED and buy.filled_qty == 10
    assert len(service.get_fills()) == 2
    logger.info("JUDGEMENT: resting bid consumed by later sell, fills=%s",
                len(service.get_fills()))


def test_no_cross_no_fill(service):
    service.submit_order("s1", "BTC", Side.SELL, "101", "5", "seller")
    buy = service.submit_order("b1", "BTC", Side.BUY, "100", "5", "buyer")
    assert buy.status == OrderStatus.NEW and buy.filled_qty == 0
    assert service.get_fills() == []
    book = service.book_snapshot("BTC")
    assert book["bids"][0]["price"] == "100"
    assert book["asks"][0]["price"] == "101"


def test_buyer_cannot_buy_without_cash(service):
    service.open_account("poor", cash="50")
    # 1 BTC @ 100 = 名义额 100 > 现金 50
    order = service.submit_order("p1", "BTC", Side.BUY, "100", "1", "poor")
    assert order.status == OrderStatus.REJECTED
    assert order.reject_reason == RejectReason.INSUFFICIENT_CASH
    # 拒绝零副作用
    assert service.get_fills() == []
    assert service.book_snapshot("BTC")["bids"] == []


def test_seller_cannot_oversell_position(service):
    # 持仓 100，卖出 101
    order = service.submit_order("s9", "BTC", Side.SELL, "100", "101", "seller")
    assert order.status == OrderStatus.REJECTED
    assert order.reject_reason == RejectReason.INSUFFICIENT_POSITION
    assert service.get_fills() == []


def test_cancel_removes_resting_order(service):
    order = service.submit_order("s1", "BTC", Side.SELL, "200", "4", "seller")
    cancelled = service.cancel_order(order.order_id)
    assert cancelled.status == OrderStatus.CANCELLED
    assert service.book_snapshot("BTC")["asks"] == []
    # 已撤单重复撤单：可区分原因
    with pytest.raises(TradingError) as ei:
        service.cancel_order(order.order_id)
    assert ei.value.reason == RejectReason.ORDER_ALREADY_CANCELLED
    # 不存在订单
    with pytest.raises(TradingError) as ei2:
        service.cancel_order("O404")
    assert ei2.value.reason == RejectReason.ORDER_NOT_FOUND


def test_cancel_filled_order_rejected(service):
    sell = service.submit_order("s1", "BTC", Side.SELL, "100", "2", "seller")
    service.submit_order("b1", "BTC", Side.BUY, "100", "2", "buyer")
    with pytest.raises(TradingError) as ei:
        service.cancel_order(sell.order_id)
    assert ei.value.reason == RejectReason.ORDER_ALREADY_FILLED


def test_self_trade_rejected_without_side_effect(service):
    # buyer 先得到 5 BTC 持仓，再自己挂卖单，随后自己的买单试图吃它
    service.deposit("buyer", symbol="BTC", quantity="5")
    service.submit_order("st1", "BTC", Side.SELL, "100", "5", "buyer")
    rejected = service.submit_order(
        "st2", "BTC", Side.BUY, "100", "5", "buyer")
    assert rejected.status == OrderStatus.REJECTED
    assert rejected.reject_reason == RejectReason.SELF_TRADE
    # 判定依据：自成交被拒，卖单与簿保持不变、无成交、资金不变
    assert service.get_fills() == []
    assert service.book_snapshot("BTC")["asks"][0]["quantity"] == "5"
    assert service.get_account("buyer").cash == 100000


def test_invalid_inputs_rejected(service):
    with pytest.raises(Exception):
        service.submit_order("x1", "BTC", Side.BUY, "0", "1", "buyer")
    with pytest.raises(Exception):
        service.submit_order("x2", "BTC", Side.BUY, "100", "0", "buyer")
    with pytest.raises(TradingError) as ei:
        service.submit_order("x3", "NOPE", Side.BUY, "1", "1", "buyer")
    assert ei.value.reason == RejectReason.UNKNOWN_SYMBOL
    with pytest.raises(TradingError) as ei2:
        service.submit_order("x4", "BTC", Side.BUY, "1", "1", "ghost")
    assert ei2.value.reason == RejectReason.UNKNOWN_ACCOUNT
