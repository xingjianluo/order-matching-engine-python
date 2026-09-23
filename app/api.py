"""HTTP 接口层。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import REASON_HTTP_STATUS, ReplayError, TradingError
from app.models import (
    AccountConfig,
    CancelOrderRequest,
    SubmitOrderRequest,
    SubmitResponse,
    CancelResponse,
)
from app.service import TradingService

logger = logging.getLogger("ome.api")


def create_app(service: TradingService | None = None) -> FastAPI:
    service = service or TradingService()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动即重放；若事件流非法/乱序，start() 抛 ReplayError，服务拒绝启动
        service.start()
        yield

    app = FastAPI(title="Local Limit-Order Matching Engine", version="0.1.0",
                  lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(TradingError)
    async def _trading_error_handler(request: Request, exc: TradingError) -> JSONResponse:
        logger.info("业务拒绝 path=%s reason=%s detail=%s",
                    request.url.path, exc.reason.value, exc.args[0] if exc.args else "")
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": exc.__class__.__name__,
                "reason": exc.reason.value,
                "detail": str(exc),
            },
        )

    @app.exception_handler(ReplayError)
    async def _replay_error_handler(request: Request, exc: ReplayError) -> JSONResponse:
        return JSONResponse(status_code=500, content={
            "error": "ReplayError", "reason": "REPLAY_CORRUPT", "detail": str(exc),
        })

    @app.post("/accounts", status_code=201)
    def configure_account(config: AccountConfig):
        return service.configure_account(config)

    @app.get("/accounts/{account_id}")
    def get_account(account_id: str):
        return service.engine.get_account(account_id)

    @app.post("/orders")
    def submit_order(req: SubmitOrderRequest):
        resp: SubmitResponse = service.engine.submit(req)
        # 幂等命中的历史拒绝也返回一致结果：accepted=false 时使用对应 HTTP 状态
        if not resp.accepted and not resp.idempotent_hit:
            status = REASON_HTTP_STATUS.get(resp.reject_reason, 400) if resp.reject_reason else 400
            return JSONResponse(status_code=status, content=resp.model_dump(mode="json"))
        return resp

    @app.post("/orders/{order_id}/cancel")
    def cancel_order(order_id: str, req: CancelOrderRequest | None = None):
        key = req.idempotency_key if req is not None else None
        resp: CancelResponse = service.engine.cancel(order_id, key)
        if not resp.cancelled and resp.reason is not None:
            status = REASON_HTTP_STATUS.get(resp.reason, 400)
            return JSONResponse(status_code=status, content=resp.model_dump(mode="json"))
        return resp

    @app.get("/orders/{order_id}")
    def get_order(order_id: str):
        return service.engine.get_order(order_id)

    @app.get("/book")
    def book():
        return service.engine.book_snapshot()

    @app.get("/trades")
    def trades():
        return service.engine.list_trades()

    return app
