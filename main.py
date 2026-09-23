"""服务入口：uvicorn main:app。"""
from app.api import create_app
from app.eventlog import EventLog
from app.service import TradingService

event_log = EventLog("data/events.jsonl")
service = TradingService(event_log=event_log)
service.replay()
app = create_app(service)
