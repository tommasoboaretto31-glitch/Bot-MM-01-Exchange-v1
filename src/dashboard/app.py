"""
Dashboard API ? FastAPI backend serving bot status,
positions, performance data, volume tracking, and real-time updates.

Run: python -m src.dashboard.app
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import urllib.parse

import uvicorn

# Add project root
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.data.storage import list_cached

logger = logging.getLogger(__name__)

app = FastAPI(title="MM Bot 01 Exchange Dashboard", version="0.2.0")

# Security: Add CORS middleware to prevent cross-origin requests from reading sensitive data
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://0.0.0.0:8000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


from src.config import DATA_DIR, ASSETS_DIR, STATIC_DIR

DATA_DIR.mkdir(parents=True, exist_ok=True)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
else:
    logger.error(f"CRITICAL: Static directory not found at {STATIC_DIR}")
    # Try a fallback if BUNDLE_DIR isn't working as expected
    alt_static = Path(__file__).parent / "static"
    if alt_static.exists():
        logger.info(f"Using fallback static directory at {alt_static}")
        app.mount("/static", StaticFiles(directory=str(alt_static)), name="static")
        STATIC_DIR = alt_static

# ? In-memory state (populated by live trader) ?

_bot_state: dict[str, Any] = {
    "status": "idle",  # idle, running, halted
    "uptime": 0,
    "start_time": None,
    "paper_mode": True,
}

_shutdown_requested = False
_is_paused = False


def reset_dashboard(capital: float):
    global _performance, _volume_data, _activity_log, _positions
    _performance = {
        "capital": capital,
        "initial_capital": capital,
        "pnl_today": 0.0,
        "total_return_pct": 0.0,
        "trades_today": 0,
        "volume": 0.0,
        "api_calls_total": 0,
        "api_calls_failed": 0,
        "orders_placed": 0,
        "orders_cancelled": 0,
    }
    _volume_data = {
        "total": 0.0,
        "per_market": {},
        "uptime_hours": 0.0,
        "start_time": time.time(),
    }
    _activity_log = []
    _positions = []
    _bot_state["start_time"] = time.time()
    _bot_state["status"] = "running"


_positions: list[dict[str, Any]] = []
_performance: dict[str, Any] = {
    "capital": 50.0,
    "initial_capital": 50.0,
    "pnl_today": 0.0,
    "total_return_pct": 0.0,
    "trades_today": 0,
    "volume": 0.0,
    "api_calls_total": 0,
    "api_calls_failed": 0,
    "orders_placed": 0,
    "orders_cancelled": 0,
}
_current_signal: dict[str, Any] = {
    "regime": "market_making",
    "bias_direction": "NEUTRAL",
    "bias_score": 0.0,
    "allow_long": True,
    "allow_short": True,
}

# Volume tracking per market
_volume_data: dict[str, Any] = {
    "total": 0.0,
    "per_market": {},  # symbol -> { volume, trades, pnl, fees }
    "uptime_hours": 0.0,
    "start_time": None,
}

_activity_log: list[dict[str, Any]] = []
_ws_clients: list[WebSocket] = []


# ? REST Endpoints ?


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve dashboard HTML."""
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>NuovoBot Dashboard</h1><p>Static files not found.</p>")


@app.get("/api/status")
async def get_status():
    return JSONResponse(
        {
            "bot": _bot_state,
            "signal": _current_signal,
            "cached_data": list_cached(),
        }
    )


@app.get("/api/positions")
async def get_positions():
    return JSONResponse({"positions": _positions})


@app.get("/api/performance")
async def get_performance():
    return JSONResponse(_performance)


@app.get("/api/volumes")
async def get_volumes():
    """Return live volume tracking data."""
    # Update uptime
    if _volume_data["start_time"]:
        elapsed = time.time() - _volume_data["start_time"]
        _volume_data["uptime_hours"] = elapsed / 3600
    return JSONResponse(_volume_data)


def _check_local_access(request: Request):
    """Restricts access to local clients only for sensitive endpoints."""
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(f"Blocking external access to sensitive API from {client_host}")
        raise HTTPException(status_code=403, detail="Access restricted to localhost")


@app.get("/api/log")
async def get_log(request: Request):
    _check_local_access(request)
    log_snapshot = list(_activity_log)
    return JSONResponse({"log": log_snapshot[-100:]})


@app.post("/api/control")
async def control(request: Request, action: dict):
    """Bot control: start, stop, update params."""
    _check_local_access(request)
    cmd = action.get("command", "")
    if cmd == "stop":
        _bot_state["status"] = "idle"
        _add_log("Bot stopped via dashboard")
    elif cmd == "start":
        _bot_state["status"] = "running"
        _bot_state["start_time"] = time.time()
        _add_log("Bot started via dashboard")
    return JSONResponse({"ok": True, "status": _bot_state["status"]})


# ? WebSocket for real-time updates ?


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Security: Prevent Cross-Site WebSocket Hijacking (CSWSH)
    origin = ws.headers.get("origin")
    if origin:
        parsed_origin = urllib.parse.urlparse(origin)
        host = ws.headers.get("host")
        if host and parsed_origin.netloc != host:
            logger.warning(
                f"Rejected WebSocket connection from unexpected origin: {origin}"
            )
            await ws.close(code=1008)
            return

    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True:
            # Update uptime
            if _volume_data["start_time"]:
                elapsed = time.time() - _volume_data["start_time"]
                _volume_data["uptime_hours"] = elapsed / 3600

            await ws.send_json(
                {
                    "bot": _bot_state,
                    "signal": _current_signal,
                    "performance": _performance,
                    "positions": _positions,
                    "volumes": _volume_data,
                }
            )
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
    except Exception:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ? Helper functions (called by live trader) ?


def update_state(
    status: str | None = None,
    positions: list[dict] | None = None,
    performance: dict | None = None,
    signal: dict | None = None,
    paper_mode: bool | None = None,
    log_msg: str | None = None,
):
    """Update dashboard state (called by trader module)."""
    global _bot_state, _positions, _performance, _current_signal
    if status:
        _bot_state["status"] = status
        if status == "running" and _bot_state.get("start_time") is None:
            _bot_state["start_time"] = time.time()
            _volume_data["start_time"] = time.time()
    if paper_mode is not None:
        _bot_state["paper_mode"] = paper_mode
    if log_msg:
        _add_log(log_msg)
        print(f"DASHBOARD_LOG: {log_msg}")
    if positions is not None:
        _positions = positions
    if performance:
        _performance.update(performance)
        # Sync total volume from performance
        if "volume" in performance:
            _volume_data["total"] = performance["volume"]
    if signal:
        _current_signal.update(signal)


def update_volume(symbol: str, trade_volume: float, trade_pnl: float, trade_fee: float):
    """Update per-market volume tracking (called by trader on each fill)."""
    if symbol not in _volume_data["per_market"]:
        _volume_data["per_market"][symbol] = {
            "volume": 0.0,
            "trades": 0,
            "pnl": 0.0,
            "fees": 0.0,
        }

    entry = _volume_data["per_market"][symbol]
    entry["volume"] += trade_volume
    entry["trades"] += 1
    entry["pnl"] += trade_pnl
    entry["fees"] += trade_fee

    # Recalculate total
    _volume_data["total"] = sum(
        m["volume"] for m in _volume_data["per_market"].values()
    )


def _add_log(msg: str):
    _activity_log.append(
        {
            "time": time.strftime("%H:%M:%S"),
            "msg": msg,
        }
    )
    if len(_activity_log) > 1000:
        _activity_log.pop(0)


# ? Main ?


async def run_dashboard(cfg=None):
    """Run the dashboard server. Suitable for asyncio.gather."""
    port = int(os.getenv("DASHBOARD_PORT", "8000"))
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")

    # Try to detect local network IP
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except:
        local_ip = "127.0.0.1"

    print("\n" + "=" * 50)
    print(f"🚀 DASHBOARD STARTED")
    print(f"Host:    http://{host}:{port}")
    if host == "0.0.0.0":
        print(f"Local:   http://localhost:{port}")
        if local_ip != "127.0.0.1":
            print(f"Network: http://{local_ip}:{port}")
        print("\n⚠️  WARNING: Dashboard is exposed to the network (0.0.0.0)!")
        print("Restrict to 127.0.0.1 for maximum security.")
    print("=" * 50 + "\n")

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(run_dashboard())
