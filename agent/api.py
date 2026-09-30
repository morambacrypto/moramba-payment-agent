"""FastAPI wrapper around the core engine (README section 6, "Surfaces").

A single-tenant service: one process, one wallet, meant to run on a
partner's own machine/network — not a multi-tenant API. The engine
(agent/engine.py) does all the real work; every route here is a thin
translation of HTTP in, `Agent` method call, HTTP out. No route
re-implements the limit check — that would defeat the entire point of
the "hands enforce limits in code" design (README section 2).

The MCP tool server (`agent/mcp_server.py`) is mounted at `/mcp` below,
so one process on one port serves both surfaces — a partner doesn't run
two separate services for the same wallet. `mcp_server._agent` is set to
the same `Agent` instance created here (see `lifespan`) rather than
letting the MCP server lazily build its own, so there's exactly one
wallet/DB connection per process, not two.
"""

import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from decimal import Decimal, InvalidOperation

from anyio import to_thread
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel
from starlette.routing import Mount

from agent import mcp_server
from agent.config import Settings
from agent.engine import Agent
from agent.ledger import PaymentRecord

logger = logging.getLogger("agent.api")

_agent: Agent | None = None

# Every route below is `def`, not `async def` — every rail (mpp/erc20/
# x402/ap2/pay_button/agent_transfer) makes blocking httpx/web3 calls, so
# sync routes are the honest signature. FastAPI/Starlette still runs each
# one concurrently, off a thread pool (`anyio.to_thread`), but that
# pool's default size (40) caps how many payments can be in flight at
# once before later requests start queuing behind it. Raise it here so
# concurrent throughput isn't capped by a default meant for general web
# handlers.
_DEFAULT_THREAD_POOL_SIZE = 100

# `Mount.app` is reassigned fresh on every lifespan start (see `lifespan`
# below) rather than built once here — a `StreamableHTTPSessionManager`
# can only be `.run()` once per instance, so the same app object can't
# survive a second startup (a real process only starts once, but the
# test suite starts/stops this same `app` many times).
_mcp_mount = Mount("/mcp", app=mcp_server.mcp.streamable_http_app(streamable_http_path="/"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    pool_size = int(os.environ.get("AGENT_THREAD_POOL_SIZE", _DEFAULT_THREAD_POOL_SIZE))
    to_thread.current_default_thread_limiter().total_tokens = pool_size
    mcp_app = mcp_server.mcp.streamable_http_app(streamable_http_path="/")
    _mcp_mount.app = mcp_app
    async with AsyncExitStack() as stack:
        # The MCP session manager's own lifespan isn't run automatically
        # just because its app is mounted — Starlette doesn't cascade
        # sub-app lifespans, so it has to be entered explicitly here.
        await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
        try:
            _agent = Agent(Settings())
        except Exception as exc:  # noqa: BLE001 - a misconfigured .env shouldn't crash-loop the service
            logger.error("failed to initialize Agent from Settings(): %s", exc)
            _agent = None
        mcp_server._agent = _agent
        try:
            yield
        finally:
            if _agent is not None:
                _agent.close()
            _agent = None
            mcp_server._agent = None


def get_agent() -> Agent:
    if _agent is None:
        raise HTTPException(status_code=503, detail="agent not initialized — check the service's .env")
    return _agent


app = FastAPI(title="Moramba Payment Agent", lifespan=lifespan)
router = APIRouter(prefix="/payment-agent-api")


def _parse_amount(raw: str) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise HTTPException(status_code=400, detail=f"invalid amount: {raw!r}") from None


def _record_to_dict(record: PaymentRecord) -> dict:
    return {
        "id": record.id,
        "created_at": record.created_at,
        "rail": record.rail,
        "recipient": record.recipient,
        "token": record.token,
        "amount": str(record.amount),
        "chain_id": record.chain_id,
        "status": record.status,
        "tx_hash": record.tx_hash,
        "signature": record.signature,
        "reason": record.reason,
        "receiving_agent_id": record.receiving_agent_id,
        "synced_at": record.synced_at,
        "sync_attempts": record.sync_attempts,
    }


class MppPayRequest(BaseModel):
    receiver_base_url: str
    receiver_agent_id: str
    amount: str
    token: str
    payment_via: str = "agent"
    payment_to: str = "agent"
    payout_agent_id: str | None = None
    to_address: str | None = None


class TransferRequest(BaseModel):
    to_address: str
    amount: str
    token: str
    token_contract_address: str | None = None
    # Normally left unset — gas is estimated live per-transaction. Only
    # needed as an explicit override when that estimate/its fallback
    # undershoots a specific token's real cost (see engine.transfer_erc20).
    gas_limit: int | None = None


class X402PayRequest(BaseModel):
    url: str
    method: str = "GET"
    token_decimals: int = 6


class Ap2PayRequest(BaseModel):
    items: list[dict]
    buyer_email: str
    buyer: dict | None = None
    delivery_address: dict | None = None
    api_key: str | None = None


class LimitsCheckRequest(BaseModel):
    recipient: str
    token: str
    amount: str
    rail: str


class PayButtonRequest(BaseModel):
    button_id: str
    network: str | None = None
    amount: str | None = None


class PayAgentRequest(BaseModel):
    receiving_agent_id: str
    amount: str
    token: str | None = None


@router.get("/health")
def health(agent: Agent = Depends(get_agent)):
    return {"status": "ok", "wallet_address": agent.wallet.address}


@router.post("/pay/mpp")
def pay_mpp(req: MppPayRequest, agent: Agent = Depends(get_agent)):
    record = agent.pay_via_mpp(
        receiver_base_url=req.receiver_base_url,
        receiver_agent_id=req.receiver_agent_id,
        amount=_parse_amount(req.amount),
        token=req.token,
        payment_via=req.payment_via,
        payment_to=req.payment_to,
        payout_agent_id=req.payout_agent_id,
        to_address=req.to_address,
    )
    return _record_to_dict(record)


@router.post("/transfer")
def transfer(req: TransferRequest, agent: Agent = Depends(get_agent)):
    record = agent.transfer_erc20(
        to_address=req.to_address,
        amount=_parse_amount(req.amount),
        token=req.token,
        token_contract_address=req.token_contract_address,
        gas_limit=req.gas_limit,
    )
    return _record_to_dict(record)


@router.post("/pay/x402")
def pay_x402(req: X402PayRequest, agent: Agent = Depends(get_agent)):
    record = agent.pay_via_x402(url=req.url, method=req.method, token_decimals=req.token_decimals)
    if record is None:
        return {"paid": False, "detail": "resource did not require payment"}
    return _record_to_dict(record)


@router.post("/pay/ap2")
def pay_ap2(req: Ap2PayRequest, agent: Agent = Depends(get_agent)):
    record = agent.pay_via_ap2(
        items=req.items,
        buyer_email=req.buyer_email,
        buyer=req.buyer,
        delivery_address=req.delivery_address,
        api_key=req.api_key,
    )
    return _record_to_dict(record)


@router.post("/pay/button")
def pay_button_route(req: PayButtonRequest, agent: Agent = Depends(get_agent)):
    record = agent.pay_via_pay_button(
        button_id=req.button_id,
        network=req.network,
        amount=_parse_amount(req.amount) if req.amount is not None else None,
    )
    return _record_to_dict(record)


@router.post("/pay/agent")
def pay_agent_route(req: PayAgentRequest, agent: Agent = Depends(get_agent)):
    record = agent.pay_agent(
        receiving_agent_id=req.receiving_agent_id,
        amount=_parse_amount(req.amount),
        token=req.token,
    )
    return _record_to_dict(record)


@router.post("/limits/check")
def check_limits(req: LimitsCheckRequest, agent: Agent = Depends(get_agent)):
    result = agent.check_spend_limits(recipient=req.recipient, token=req.token, amount=_parse_amount(req.amount), rail=req.rail)
    return {"allowed": result.allowed, "reason": result.reason}


@router.get("/payments")
def list_payments(
    since: str | None = None,
    recipient: str | None = None,
    rail: str | None = None,
    limit: int = 100,
    agent: Agent = Depends(get_agent),
):
    records = agent.history(since=since, recipient=recipient, rail=rail, limit=limit)
    return [_record_to_dict(r) for r in records]


app.include_router(router)
app.router.routes.append(_mcp_mount)
