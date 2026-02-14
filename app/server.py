from __future__ import annotations

import asyncio
import os
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .clock import RealClock
from .config import BrainConfig
from .dashboard_data import build_dashboard_summary, build_repo_map
from .metrics import CompositeMetrics, Metrics
from .orchestrator import Orchestrator
from .provider import build_llm_client
from .prom_export import GLOBAL_PROM
from .security import is_ip_allowed, resolve_client_ip, verify_query_token, verify_shared_secret
from .shell.executor import ShellExecutor
from .trace import TraceSink
from .transport_ws import GateRef, Transport, socket_reader, socket_writer
from .bounded_queue import BoundedDequeQueue
from .tools import ToolRegistry


class StarletteTransport(Transport):
    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def recv_text(self) -> str:
        return await self._ws.receive_text()

    async def send_text(self, text: str) -> None:
        await self._ws.send_text(text)

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        try:
            await self._ws.close(code=code, reason=reason)
        except Exception:
            return


app = FastAPI()
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DASHBOARD_DIR = _REPO_ROOT / "dashboard"
if _DASHBOARD_DIR.exists():
    app.mount("/dashboard", StaticFiles(directory=str(_DASHBOARD_DIR), html=True), name="dashboard")


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/metrics")
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(GLOBAL_PROM.render())


@app.get("/api/dashboard/summary")
async def dashboard_summary() -> JSONResponse:
    payload = build_dashboard_summary(GLOBAL_PROM.render())
    return JSONResponse(payload)


@app.get("/api/dashboard/repo-map")
async def dashboard_repo_map() -> JSONResponse:
    return JSONResponse(build_repo_map(_REPO_ROOT))


@app.get("/api/dashboard/sop")
async def dashboard_sop() -> JSONResponse:
    path = _REPO_ROOT / "docs" / "self_improve_sop.md"
    text = ""
    ok = False
    if path.exists():
        ok = True
        text = path.read_text(encoding="utf-8")
    return JSONResponse(
        {
            "ok": ok,
            "path": "docs/self_improve_sop.md",
            "markdown": text,
        }
    )


@app.get("/api/dashboard/readme")
async def dashboard_readme() -> JSONResponse:
    path = _REPO_ROOT / "README.md"
    text = ""
    ok = False
    if path.exists():
        ok = True
        text = path.read_text(encoding="utf-8")
    return JSONResponse(
        {
            "ok": ok,
            "path": "README.md",
            "markdown": text,
        }
    )

@app.websocket("/llm-websocket/{call_id}")
async def llm_websocket(ws: WebSocket, call_id: str) -> None:
    await _run_session(ws, call_id, route_name="llm-websocket")


def _normalize_route(route: str) -> str:
    return str(route or "").strip().strip("/").strip()


def _log_ws_event(cfg: "BrainConfig", *, route_name: str, call_id: str, event: str, **payload: object) -> None:
    if not cfg.ws_structured_logging:
        return
    base = {
        "component": "ws_session",
        "event": event,
        "route": f"/{_normalize_route(route_name)}",
        "call_id": call_id,
    }
    base.update(payload)
    print(json.dumps(base, sort_keys=True, separators=(",", ":")))


async def _run_session(ws: WebSocket, call_id: str, route_name: str) -> None:
    cfg = BrainConfig.from_env()
    route = _normalize_route(route_name)
    # Canonical contract: production route is fixed and must not drift.
    canonical_route = "llm-websocket"
    if route != canonical_route:
        await ws.accept()
        _log_ws_event(
            cfg,
            route_name=route,
            call_id=call_id,
            event="reject_noncanonical_route",
            canonical_route=f"/{canonical_route}",
        )
        await ws.close(code=1008, reason="non_canonical_route")
        return
    _log_ws_event(
        cfg,
        route_name=route,
        call_id=call_id,
        event="connect",
        canonical_route=f"/{canonical_route}",
        ip="",
    )
    # Optional WS handshake hardening. In production, prefer enforcing at the reverse proxy.
    headers = {str(k): str(v) for k, v in ws.headers.items()}
    remote_ip = ""
    try:
        if ws.client is not None:
            remote_ip = ws.client.host or ""
    except Exception:
        remote_ip = ""

    effective_ip = resolve_client_ip(
        remote_ip=remote_ip,
        headers=headers,
        trusted_proxy_enabled=cfg.ws_trusted_proxy_enabled,
        trusted_proxy_cidrs=cfg.ws_trusted_proxy_cidrs,
    )

    if cfg.ws_allowlist_enabled and not is_ip_allowed(
        remote_ip=effective_ip, cidrs=cfg.ws_allowlist_cidrs
    ):
        await ws.accept()
        await ws.close(code=1008, reason="forbidden")
        return

    if cfg.ws_shared_secret_enabled and not verify_shared_secret(
        headers=headers,
        header=cfg.ws_shared_secret_header,
        secret=cfg.ws_shared_secret,
    ):
        await ws.accept()
        await ws.close(code=1008, reason="forbidden")
        return

    if not verify_query_token(
        query_params=dict(ws.query_params),
        token_param=cfg.ws_query_token_param,
        expected_token=cfg.ws_query_token,
    ):
        await ws.accept()
        await ws.close(code=1008, reason="forbidden")
        return

    await ws.accept()
    clock = RealClock()
    session_metrics = Metrics()
    metrics = CompositeMetrics(session_metrics, GLOBAL_PROM)
    trace = TraceSink()

    inbound_q: BoundedDequeQueue = BoundedDequeQueue(maxsize=cfg.inbound_queue_max)
    outbound_q: BoundedDequeQueue = BoundedDequeQueue(maxsize=cfg.outbound_queue_max)
    shutdown_evt = asyncio.Event()
    gate = GateRef(epoch=0, speak_gen=0)
    shell_executor = ShellExecutor(
        mode=cfg.shell_mode,
        enable_hosted=cfg.shell_enable_hosted,
        allowed_commands=cfg.shell_allowed_commands,
        workdir=os.getcwd(),
    )
    tools = ToolRegistry(
        session_id=call_id,
        clock=clock,
        metrics=metrics,
        shell_executor=shell_executor,
        shell_tool_enabled=cfg.shell_tool_enabled,
        shell_tool_canary_enabled=cfg.shell_tool_canary_enabled,
        shell_tool_canary_percent=cfg.shell_tool_canary_percent,
    )
    llm = build_llm_client(cfg, session_id=call_id)

    transport = StarletteTransport(ws)
    orch = Orchestrator(
        session_id=call_id,
        call_id=call_id,
        config=cfg,
        clock=clock,
        metrics=metrics,
        trace=trace,
        inbound_q=inbound_q,
        outbound_q=outbound_q,
        shutdown_evt=shutdown_evt,
        gate=gate,
        tools=tools,
        llm=llm,
    )

    reader_task = asyncio.create_task(
        socket_reader(
            transport=transport,
            inbound_q=inbound_q,
            metrics=metrics,
            shutdown_evt=shutdown_evt,
            max_frame_bytes=cfg.ws_max_frame_bytes,
            structured_logs=cfg.ws_structured_logging,
            call_id=call_id,
        )
    )
    writer_task = asyncio.create_task(
        socket_writer(
            transport=transport,
            outbound_q=outbound_q,
            metrics=metrics,
            shutdown_evt=shutdown_evt,
            gate=gate,
            clock=clock,
            inbound_q=inbound_q,
            ws_write_timeout_ms=cfg.ws_write_timeout_ms,
            ws_close_on_write_timeout=cfg.ws_close_on_write_timeout,
            ws_max_consecutive_write_timeouts=cfg.ws_max_consecutive_write_timeouts,
        )
    )
    orch_task = asyncio.create_task(orch.run())

    try:
        await orch_task
    finally:
        shutdown_evt.set()
        reader_task.cancel()
        writer_task.cancel()
        if llm is not None:
            await llm.aclose()
        await transport.close(code=1000, reason="session_end")
