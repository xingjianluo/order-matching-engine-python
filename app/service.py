"""应用服务层：装配事件存储与引擎，启动时重放恢复状态。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from app.engine import MatchingEngine
from app.models import AccountConfig, AccountView
from app.store import EventStore

logger = logging.getLogger("ome.service")

DEFAULT_STORE_PATH = os.environ.get("OME_STORE_PATH", str(Path("data") / "events.jsonl"))


class TradingService:
    def __init__(self, store_path: str | os.PathLike[str] = DEFAULT_STORE_PATH) -> None:
        self.store_path = Path(store_path)
        self.store = EventStore(self.store_path)
        self.engine = MatchingEngine(self.store)
        self._started = False

    def start(self) -> None:
        """确保存储存在并依据已持久化事件重建状态（幂等，可重复调用）。"""
        if self._started:
            return
        self.store.ensure()
        self.engine.replay()
        self._started = True
        logger.info("服务就绪，事件日志 %s", self.store_path)

    def configure_account(self, config: AccountConfig) -> AccountView:
        return self.engine.configure_account(config)
