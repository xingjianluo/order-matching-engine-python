"""共享夹具与日志：测试日志打印输入、关键状态与判定依据。"""
import logging
import sys

import pytest

from app.service import TradingService

logging.basicConfig(
    level=logging.INFO, stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


@pytest.fixture
def service():
    """纯内存交易服务 + 两个标准账户与 BTC 品种。"""
    s = TradingService()
    s.open_account("buyer", cash="100000")
    s.open_account("seller", cash="0", max_position={"BTC": "1000"})
    s.register_symbol("BTC")
    s.deposit("seller", symbol="BTC", quantity="100")
    return s
