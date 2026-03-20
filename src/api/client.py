"""01 Exchange REST API client with session-based auth."""

from __future__ import annotations

import binascii
import json
import logging
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from base58 import b58encode, b58decode
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.api import schema_pb2
from src.config import MarketInfo

logger = logging.getLogger(__name__)

def encode_varint(value: int) -> bytes:
    buf = bytearray()
    while value >= 0x80:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value)
    return bytes(buf)

def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        byte = data[offset]
        result |= (byte & 0x7F) << shift
        offset += 1
        if not (byte & 0x80):
            break
        shift += 7
    return result, offset


@dataclass
class OrderResult:
    order_id: int
    market_id: int
    side: str
    size: float
    price: float
    status: str


class O1Client:
    """Async REST client for 01 Exchange."""

    def __init__(self, api_url: str = "https://zo-mainnet.n1.xyz",
                 keypair_path: str | None = None):
        self.api_url = api_url.rstrip("/")
        self._session_id: str | None = None
        self._session_key: Ed25519PrivateKey | None = None
        self._user_key: Ed25519PrivateKey | None = None
        self._user_pubkey: bytes | None = None
        self._markets: dict[str, MarketInfo] = {}
        self._nonce_counter = 0
        self._lock = asyncio.Lock()  # Prevent concurrent session creation
        self.stats = {
            "api_calls_total": 0,
            "api_calls_failed": 0,
            "orders_placed": 0,
            "orders_cancelled": 0,
        }
        
        self.session: aiohttp.ClientSession | None = None

        if keypair_path and Path(keypair_path).exists():
            self._load_keypair(keypair_path)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"Content-Type": "application/json"})
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def _load_keypair(self, path: str) -> None:
        """Load Solana keypair from JSON file.
        
        Supports two formats:
        1. Simple object: {"PRIVATE_KEY": "<base58string>"}
        2. Legacy array:  [12, 34, 56, ... ] (64 numbers)
        """
        with open(path, "r") as f:
            key_data = json.load(f)

        if isinstance(key_data, dict):
            # Format 1: {"PRIVATE_KEY": "<base58string>"}
            raw = b58decode(key_data["PRIVATE_KEY"])
            private_bytes = raw[:32]
        else:
            # Format 2: legacy JSON array of 64 numbers
            private_bytes = bytes(key_data[:32])

        self._user_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        self._user_pubkey = self._user_key.public_key().public_bytes_raw()
        logger.info("Loaded keypair: %s", b58encode(self._user_pubkey).decode())

    async def _request(self, method: str, path: str, retries: int = 3, **kwargs) -> Any:
        session = await self._get_session()
        url = f"{self.api_url}{path}"
        
        for attempt in range(retries):
            self.stats["api_calls_total"] += 1
            try:
                async with session.request(method, url, **kwargs) as resp:
                    resp.raise_for_status()
                    
                    if resp.content_type == "application/json":
                        return await resp.json()
                    return await resp.read()
            except Exception as e:
                self.stats["api_calls_failed"] += 1
                if attempt == retries - 1:
                    raise e
                # Exponential backoff
                await asyncio.sleep(0.5 * (2 ** attempt))

    # ? Public endpoints (no auth) ?

    async def get_info(self) -> dict[str, Any]:
        """GET /info ? exchange info, markets, tokens."""
        data = await self._request("GET", "/info")
        # Cache market info
        for m in data.get("markets", []):
            mi = MarketInfo(
                market_id=m["marketId"],
                symbol=m["symbol"],
                price_decimals=m["priceDecimals"],
                size_decimals=m["sizeDecimals"],
                imf=m["imf"],
                mmf=m["mmf"],
            )
            self._markets[mi.symbol] = mi
        return data

    async def get_markets(self) -> dict[str, MarketInfo]:
        """Return cached market info (calls get_info if empty)."""
        if not self._markets:
            await self.get_info()
        return self._markets

    async def get_orderbook(self, market_id: int) -> dict[str, Any]:
        """GET /market/{id}/orderbook ? L2 orderbook snapshot."""
        return await self._request("GET", f"/market/{market_id}/orderbook")

    async def get_trades(self, market_id: int | None = None) -> list[dict]:
        """GET /trades ? recent trades."""
        path = "/trades"
        if market_id is not None:
            path += f"?marketId={market_id}"
        return await self._request("GET", path)

    async def get_market_stats(self, market_id: int) -> dict[str, Any]:
        """GET /market/{id}/stats ? 24h stats."""
        return await self._request("GET", f"/market/{market_id}/stats")

    async def get_timestamp(self) -> int:
        """GET /timestamp ? server timestamp."""
        data = await self._request("GET", "/timestamp")
        
        if isinstance(data, str):
            from datetime import datetime
            time_str = data.replace("Z", "+00:00")
            dt = datetime.fromisoformat(time_str)
            return int(dt.timestamp())
            
        try:
            val = int(data) # type: ignore
            # Handle millisecond timestamps (e.g. 1,735,689,600,000)
            if val > 50_000_000_000: 
                return val // 1000
            return val
        except (TypeError, ValueError):
            return 0

    async def get_user(self, pubkey: str) -> dict[str, Any]:
        """GET /user/{pubkey} ? user info & accounts."""
        return await self._request("GET", f"/user/{pubkey}")

    async def get_account(self, account_id: int) -> dict[str, Any]:
        """GET /account/{id} ? account details."""
        return await self._request("GET", f"/account/{account_id}")

    # ? Market helpers ?

    async def market_by_symbol(self, symbol: str) -> MarketInfo | None:
        """Lookup market by symbol (e.g. 'HYPEUSD')."""
        markets = await self.get_markets()
        return markets.get(symbol)

    @property
    def user_pubkey_b58(self) -> str | None:
        if self._user_pubkey:
            return b58encode(self._user_pubkey).decode()
        return None

    # ? Authenticated endpoints ?

    def is_authenticated(self) -> bool:
        return self._session_id is not None

    def _user_sign(self, message: bytes) -> bytes:
        return self._user_key.sign(message)

    def _session_sign(self, message: bytes) -> bytes:
        if not self._session_key:
            raise RuntimeError("Session not initialized.")
        return self._session_key.sign(message)

    async def _execute_action(self, action: schema_pb2.Action, sign_func) -> schema_pb2.Receipt:
        payload = action.SerializeToString()
        length_prefix = encode_varint(len(payload))
        message = length_prefix + payload
        signature = sign_func(message)
        final_data = message + signature
        
        session = await self._get_session()
        url = f"{self.api_url}/action"
        
        for attempt in range(2):
            self.stats["api_calls_total"] += 1
            try:
                async with session.post(url, data=final_data, headers={"Content-Type": "application/octet-stream"}) as resp:
                    response_data = await resp.read()
                    if resp.status != 200:
                        self.stats["api_calls_failed"] += 1
                        try:
                            msg_len, pos = decode_varint(response_data, 0)
                            actual = bytes(response_data[pos : pos + msg_len])
                            receipt = schema_pb2.Receipt()
                            receipt.ParseFromString(actual)
                            logger.error(f"Action failed. HTTP {resp.status}, Receipt: {receipt}")
                        except Exception:
                            logger.error(f"Action failed. HTTP {resp.status}, Body: {response_data}")
                        resp.raise_for_status()

                    msg_len, pos = decode_varint(response_data, 0)
                    actual_data = bytes(response_data[pos : pos + msg_len])
                    receipt = schema_pb2.Receipt()
                    receipt.ParseFromString(bytes(actual_data))
                    return receipt
            except aiohttp.ClientError as e:
                self.stats["api_calls_failed"] += 1
                if attempt == 1:
                    raise e
                await asyncio.sleep(0.5)
        raise RuntimeError("Failed to execute action")

    async def create_session(self) -> str:
        """Create authenticated session and generate session keys."""
        if not self._user_key:
            raise RuntimeError("Cannot create session: id.json keypair not loaded.")
            
        self._session_key = Ed25519PrivateKey.generate()
        session_pubkey = self._session_key.public_key().public_bytes_raw()
        
        server_time = await self.get_timestamp()
        expiry = server_time + 3600
        
        action = schema_pb2.Action()
        action.current_timestamp = server_time
        self._nonce_counter += 1
        action.nonce = self._nonce_counter
        
        action.create_session.CopyFrom(
            schema_pb2.Action.CreateSession(
                user_pubkey=self._user_pubkey,
                session_pubkey=session_pubkey,
                expiry_timestamp=expiry,
                signature_framing=schema_pb2.Action.HEX,
            )
        )
        
        async with self._lock:
            if self._session_id:
                return str(self._session_id)
                
            receipt = await self._execute_action(action, self._user_sign)
            if receipt.HasField("err"):
                raise RuntimeError(f"Session creation failed: {schema_pb2.Error.Name(receipt.err) if hasattr(schema_pb2.Error, 'Name') else receipt.err}")
                
            self._session_id = receipt.create_session_result.session_id
            logger.info(f"01 Session created: {self._session_id}")
            return str(self._session_id)

    async def place_order(self, market_id: int, side: str, size: float,
                    price: float, order_type: str = "limit", reduce_only: bool = False) -> OrderResult:
        """Place an order using authenticated Protobuf Action."""
        if not self.is_authenticated():
            await self.create_session()
            
        mi = await self.get_markets()
        market_info = None
        for m in mi.values():
            if m.market_id == market_id:
                market_info = m
                break
        if not market_info:
            raise ValueError(f"Market ID {market_id} not found in exchange info.")
            
        price_dec = market_info.price_decimals # type: ignore
        size_dec = market_info.size_decimals # type: ignore
        
        raw_price = int(price * (10 ** price_dec))
        raw_size = int(size * (10 ** size_dec))
        
        proto_fill_mode = schema_pb2.POST_ONLY if order_type == "post_only" else schema_pb2.IMMEDIATE_OR_CANCEL if order_type == "immediate" else schema_pb2.LIMIT
        proto_side = schema_pb2.BID if side.lower() == "buy" else schema_pb2.ASK
        
        server_time = await self.get_timestamp()
        if server_time > 2_000_000_000_000:
            server_time = server_time // 1000
            
        action = schema_pb2.Action()
        action.current_timestamp = server_time
        self._nonce_counter += 1
        action.nonce = self._nonce_counter
        
        action.place_order.CopyFrom(
            schema_pb2.Action.PlaceOrder(
                session_id=self._session_id,
                market_id=market_id,
                side=proto_side,
                price=raw_price,
                size=raw_size,
                fill_mode=proto_fill_mode,
                is_reduce_only=reduce_only,
            )
        )
        
        receipt = await self._execute_action(action, self._session_sign)
        if receipt.HasField("err"):
            error_name = str(receipt.err)
            try:
                error_name = schema_pb2.Error.Name(receipt.err)
            except Exception:
                pass
            if "SESSION" in error_name.upper():
                logger.warning(f"Session expired ({error_name}), recreating...")
                self._session_id = None
                return await self.place_order(market_id, side, size, price, order_type, reduce_only)
            raise RuntimeError(f"Order placement rejected: {error_name}")
            
        result = receipt.trade_or_place
        order_id = result.posted.order_id if result.HasField("posted") else 0
        fill_status = "OPEN" if order_id > 0 else "FILLED"
        
        if order_id > 0:
            self.stats["orders_placed"] += 1
        
        return OrderResult(
            order_id=order_id,
            market_id=market_id,
            side=side.lower(),
            size=size,
            price=price,
            status=fill_status
        )

    async def cancel_order(self, order_id: int) -> dict:
        """Cancel an existing order on 01 exchange."""
        if not self.is_authenticated():
            await self.create_session()
            
        server_time = await self.get_timestamp()
        if server_time > 2_000_000_000_000:
            server_time = server_time // 1000
            
        action = schema_pb2.Action()
        action.current_timestamp = server_time
        self._nonce_counter += 1
        action.nonce = self._nonce_counter
        
        action.cancel_order_by_id.CopyFrom(
            schema_pb2.Action.CancelOrderById(
                session_id=self._session_id,
                order_id=order_id,
            )
        )
        
        receipt = await self._execute_action(action, self._session_sign)
        if receipt.HasField("err"):
            error_name = str(receipt.err)
            try:
                error_name = schema_pb2.Error.Name(receipt.err)
            except Exception:
                pass
            if "NOT_FOUND" in error_name.upper():
                return {"success": False, "error": error_name}
            raise RuntimeError(f"Cancel order rejected: {error_name}")
            
        self.stats["orders_cancelled"] += 1
        return {"success": True, "order_id": order_id}

    async def cancel_all(self, market_id: int) -> dict:
        """Not directly supported as a single protobuf action safely. Must tracking order_ids."""
        return {"success": False, "error": "NOT_IMPLEMENTED"}
