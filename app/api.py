"""HTTP 接口层（FastAPI 路由）。

业务拒绝（TradingError）统一返回 422 + reason 码，调用方可据 reason 区分
自成交、超限、重复提交、重复撤单等不同原因。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.models import OrderStatus, RejectReason, Side, TradingError
from app.service import TradingService


# ---------- 请求/响应模型 ----------
class SubmitOrderRequest(BaseModel):
    client_order_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    side: Side
    price: str = Field(description="正数，字符串十进制，如 '100.5'")
    quantity: str = Field(description="正数，字符串十进制，如 '2.5'")
    account: str = Field(min_length=1)


class CancelOrderRequest(BaseModel):
    order_id: str = Field(min_length=1)


class OpenAccountRequest(BaseModel):
    account_id: str = Field(min_length=1)
    cash: str = "0"
    max_position: dict[str, str] | None = None
    max_order_notional: str = "Infinity"


class DepositRequest(BaseModel):
    account_id: str = Field(min_length=1)
    symbol: str | None = None
    quantity: str = "0"
    cash: str = "0"


class AdjustLimitsRequest(BaseModel):
    # 只携带需要调整的品种；值为 null 表示移除该品种限额
    max_position: dict[str, str | None] | None = None
    max_order_notional: str | None = None


def _order_json(order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "price": str(order.price),
        "quantity": str(order.quantity),
        "filled_quantity": str(order.filled_qty),
        "remaining_quantity": str(order.remaining),
        "account": order.account,
        "status": order.status.value,
        "reject_reason": order.reject_reason.value if order.reject_reason else None,
    }


def _fill_json(fill) -> dict[str, Any]:
    return {
        "fill_id": fill.fill_id,
        "sequence": fill.sequence,
        "symbol": fill.symbol,
        "price": str(fill.price),
        "quantity": str(fill.quantity),
        "buy_order_id": fill.buy_order_id,
        "sell_order_id": fill.sell_order_id,
        "taker_order_id": fill.taker_order_id,
        "buy_account": fill.buy_account,
        "sell_account": fill.sell_account,
    }


def _account_json(acc) -> dict[str, Any]:
    return {
        "account_id": acc.account_id,
        "cash": str(acc.cash),
        "positions": {s: str(q) for s, q in acc.positions.items()},
        "max_position": {s: str(q) for s, q in acc.max_position.items()},
        "max_order_notional": str(acc.max_order_notional),
    }


def _risk_snapshot_json(snap) -> dict[str, Any]:
    return {
        "account_id": snap.account_id,
        "cash": str(snap.cash),
        "reserved_buy_notional": str(snap.reserved_buy_notional),
        "reserved_sell_notional": str(snap.reserved_sell_notional),
        "available_cash": str(snap.available_cash),
        "projected_cash": str(snap.projected_cash),
        "max_order_notional": str(snap.max_order_notional),
        "cash_limit_breached": snap.cash_limit_breached,
        "projected_cash_limit_breached": snap.projected_cash_limit_breached,
        "order_notional_breaches": list(snap.order_notional_breaches),
        "any_breach": snap.any_breach,
        "positions": [
            {
                "symbol": v.symbol,
                "position": str(v.position),
                "reserved_sell": str(v.reserved_sell),
                "available_position": str(v.available_position),
                "max_position": str(v.max_position) if v.max_position is not None
                else None,
                "projected_position": str(v.projected_position),
                "position_limit_breached": v.position_limit_breached,
                "projected_position_limit_breached":
                    v.projected_position_limit_breached,
            }
            for v in snap.positions.values()
        ],
    }


def create_app(service: TradingService) -> FastAPI:
    app = FastAPI(title="Local Limit-Order Matching Engine", version="0.1.0")
    app.state.service = service

    @app.exception_handler(TradingError)
    async def _trading_error_handler(_request: Request, exc: TradingError):
        return JSONResponse(
            status_code=422,
            content={"error": exc.reason.value, "reason": exc.reason.value,
                     "message": str(exc)},
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "symbols": service.list_symbols()}

    @app.post("/admin/accounts")
    def open_account(req: OpenAccountRequest):
        acc = service.open_account(
            req.account_id, cash=Decimal(req.cash),
            max_position=req.max_position,
            # pydantic 会把 "Infinity" 解析为 float，统一转回 Decimal
            max_order_notional=Decimal(str(req.max_order_notional)))
        return _account_json(acc)

    @app.post("/admin/symbols/{symbol}")
    def register_symbol(symbol: str):
        service.register_symbol(symbol)
        return {"symbol": symbol, "registered": True}

    @app.post("/admin/deposits")
    def deposit(req: DepositRequest):
        service.deposit(req.account_id, symbol=req.symbol,
                        quantity=Decimal(req.quantity), cash=Decimal(req.cash))
        return {"accepted": True}

    @app.post("/admin/accounts/{account_id}/limits")
    def adjust_limits(account_id: str, req: AdjustLimitsRequest):
        acc = service.adjust_limits(
            account_id,
            max_position=req.max_position,
            max_order_notional=(Decimal(str(req.max_order_notional))
                                if req.max_order_notional is not None else None))
        return _account_json(acc)

    @app.post("/orders")
    def submit_order(req: SubmitOrderRequest):
        order = service.submit_order(
            client_order_id=req.client_order_id, symbol=req.symbol,
            side=req.side, price=req.price, quantity=req.quantity,
            account=req.account)
        return _order_json(order)

    @app.post("/orders/cancel")
    def cancel_order(req: CancelOrderRequest):
        return _order_json(service.cancel_order(req.order_id))

    @app.get("/orders/{order_id}")
    def get_order(order_id: str):
        return _order_json(service.get_order(order_id))

    @app.get("/fills")
    def list_fills(symbol: str | None = None):
        return {"fills": [_fill_json(f) for f in service.get_fills(symbol)]}

    @app.get("/books/{symbol}")
    def book_snapshot(symbol: str):
        return service.book_snapshot(symbol)

    @app.get("/accounts/{account_id}")
    def get_account(account_id: str):
        return _account_json(service.get_account(account_id))

    @app.get("/accounts/{account_id}/risk")
    def account_risk_snapshot(account_id: str):
        return _risk_snapshot_json(service.risk_snapshot(account_id))

    return app
