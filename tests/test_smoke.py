import os

from fastapi.testclient import TestClient

from app.api import create_app
from app.service import TradingService


def _client(tmp_path) -> TestClient:
    svc = TradingService(store_path=tmp_path / "events.jsonl")
    application = create_app(svc)
    return TestClient(application)


def test_http_end_to_end(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/accounts", json={
            "account_id": "A", "cash": "1000", "position": "0",
            "max_order_notional": "900", "max_net_position": "10"})
        assert r.status_code == 201, r.text
        r = c.post("/accounts", json={
            "account_id": "B", "cash": "1000", "position": "5",
            "max_order_notional": "900", "max_net_position": "10"})
        assert r.status_code == 201, r.text

        r = c.post("/orders", json={
            "account_id": "B", "side": "SELL", "price": "100",
            "quantity": "5", "idempotency_key": "sell-1"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "NEW"

        r = c.post("/orders", json={
            "account_id": "A", "side": "BUY", "price": "100",
            "quantity": "2", "idempotency_key": "buy-1"})
        body = r.json()
        assert body["status"] == "FILLED"
        assert len(body["trades"]) == 1
        assert body["trades"][0]["quantity"] == "2"

        # 幂等重复：不再成交，除 idempotent_hit 标记外与首次响应一致
        r2 = c.post("/orders", json={
            "account_id": "A", "side": "BUY", "price": "100",
            "quantity": "2", "idempotency_key": "buy-1"})
        repeat = r2.json()
        assert repeat["idempotent_hit"] is True
        repeat["idempotent_hit"] = False
        assert repeat == body

        # 余额与持仓变化
        a = c.get("/accounts/A").json()
        b = c.get("/accounts/B").json()
        assert a["cash"] == "800" and a["position"] == "2"
        assert b["cash"] == "1200" and b["position"] == "3"

    # 重启：新的 service/app 读同一事件文件
    with _client(tmp_path) as c:
        trades = c.get("/trades").json()
        assert len(trades) == 1
        # 重启后幂等仍然成立，不会重复成交
        r = c.post("/orders", json={
            "account_id": "A", "side": "BUY", "price": "100",
            "quantity": "2", "idempotency_key": "buy-1"})
        assert r.json()["idempotent_hit"] is True
        assert len(r.json()["trades"]) == 1
        assert len(c.get("/trades").json()) == 1
